"""Twenty CRM adapter for the Real-App Verification Matrix."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from benchmarks.real_apps.apps.twenty_crm.fault_injectors import (
    optimistic_ui_rollback_rule,
)
from benchmarks.real_apps.apps.twenty_crm.readback import readback_twenty_opportunity
from benchmarks.real_apps.apps.twenty_crm.seed import (
    OpportunityFixture,
    default_opportunity,
)
from benchmarks.real_apps.fault_proxy import FaultProxy, FaultRule
from benchmarks.real_apps.harness import (
    AppRunResult,
    ReadbackResult,
    RealAppSpec,
    WorkflowSpec,
)

TWENTY_CRM_SPEC = RealAppSpec(
    app_id="twenty_crm",
    display_name="Twenty CRM",
    vertical="sales-crm",
    surface="web",
    ui_architecture="react-spa-graphql-optimistic",
    persistence_oracle="graphql+postgres",
    compose_file=Path(__file__).resolve().parent / "docker-compose.yml",
    tier="nightly",
    workflows=(
        WorkflowSpec(
            workflow_id="create_opportunity",
            description="Create a synthetic CRM opportunity and verify via GraphQL read-back",
            expected_writes=1,
            fault_mode="optimistic_ui_rollback",
        ),
    ),
)


class _InProcessTwentyGraphQLServer:
    """Minimal local GraphQL server for contract-mode verification."""

    def __init__(self) -> None:
        self.opportunities: dict[str, dict[str, Any]] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        host_str = host.decode("ascii") if isinstance(host, bytes) else str(host)
        return f"http://{host_str}:{port}"

    def __enter__(self) -> _InProcessTwentyGraphQLServer:
        store = self.opportunities

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or "0")
                doc = json.loads(self.rfile.read(length).decode("utf-8"))
                op = doc.get("operationName")
                variables = doc.get("variables") or {}
                if op == "CreateOpportunity":
                    item = dict(variables.get("input") or {})
                    store[str(item["id"])] = item
                    resp_doc: dict[str, Any] = {"data": {"createOpportunity": item}}
                elif op == "GetOpportunity":
                    opp_id = str(variables.get("id") or "")
                    resp_doc = {"data": {"opportunity": store.get(opp_id)}}
                else:
                    resp_doc = {"errors": [{"message": f"Unknown operation {op!r}"}]}
                raw = json.dumps(resp_doc).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

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


def _post_gql(url: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10.0) as resp:
        return int(resp.status), json.loads(resp.read().decode("utf-8"))


class TwentyCRMAdapter:
    """RealAppAdapter for Twenty CRM."""

    spec = TWENTY_CRM_SPEC

    def run_contract_verification(self, work_dir: Path) -> AppRunResult:
        t0 = time.monotonic()
        happy_opp = default_opportunity()
        fault_opp = OpportunityFixture(
            opportunity_id="opp-syn-fault-2002",
            name="Synthetic Rollback Deal",
            contact_email="rollback@synthetic.example",
            stage="PROPOSAL",
            amount_micros=75_000_000_000,
        )

        with _InProcessTwentyGraphQLServer() as backend, FaultProxy(backend.base_url) as proxy:
            proxy.set_rule(FaultRule(mode="none"))
            _post_gql(f"{proxy.base_url}/graphql", happy_opp.to_graphql_mutation())
            happy_rb = readback_twenty_opportunity(
                f"{backend.base_url}/graphql",
                opportunity_id=happy_opp.opportunity_id,
                expected_name=happy_opp.name,
                expected_stage=happy_opp.stage,
                expected_amount_micros=happy_opp.amount_micros,
            )

            proxy.set_rule(
                optimistic_ui_rollback_rule(fault_opp.opportunity_id, fault_opp.name)
            )
            fault_status, fault_body = _post_gql(
                f"{proxy.base_url}/graphql", fault_opp.to_graphql_mutation()
            )
            fault_rb = readback_twenty_opportunity(
                f"{backend.base_url}/graphql",
                opportunity_id=fault_opp.opportunity_id,
                expected_name=fault_opp.name,
                expected_stage=fault_opp.stage,
                expected_amount_micros=fault_opp.amount_micros,
            )

        deceptive_ui_ok = (
            fault_status == 200
            and isinstance((fault_body.get("data") or {}).get("createOpportunity"), dict)
        )
        return AppRunResult(
            app_id=self.spec.app_id,
            display_name=self.spec.display_name,
            vertical=self.spec.vertical,
            surface=self.spec.surface,
            tier=self.spec.tier,
            mode="contract",
            happy_path_verified=happy_rb.confirmed,
            silent_fault_caught=(deceptive_ui_ok and not fault_rb.confirmed),
            fault_mode_tested="optimistic_ui_rollback",
            elapsed_s=time.monotonic() - t0,
            happy_readback=happy_rb,
            fault_readback=fault_rb,
        )

    def run_live_verification(self, work_dir: Path) -> AppRunResult:
        t0 = time.monotonic()
        gql_url = os.environ.get("TWENTY_GRAPHQL_URL", "").strip()
        if not gql_url:
            rb = ReadbackResult(
                confirmed=False,
                oracle="twenty-graphql",
                reason="TWENTY_GRAPHQL_URL is not set; start docker-compose.yml first",
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
                fault_mode_tested="optimistic_ui_rollback",
                elapsed_s=time.monotonic() - t0,
                happy_readback=rb,
                fault_readback=rb,
            )
        opp = default_opportunity()
        rb = readback_twenty_opportunity(
            gql_url,
            opportunity_id=opp.opportunity_id,
            expected_name=opp.name,
            expected_stage=opp.stage,
            expected_amount_micros=opp.amount_micros,
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
            fault_mode_tested="optimistic_ui_rollback",
            elapsed_s=time.monotonic() - t0,
            happy_readback=rb,
            fault_readback=ReadbackResult(
                confirmed=False,
                oracle="twenty-graphql",
                reason="Live fault verification requires proxy routing",
            ),
        )
