"""Odoo 17 adapter for the Real-App Verification Matrix."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from benchmarks.real_apps.apps.odoo17.fault_injectors import silent_bill_post_drop_rule
from benchmarks.real_apps.apps.odoo17.readback import readback_odoo_vendor_bill
from benchmarks.real_apps.apps.odoo17.seed import VendorBillFixture, default_vendor_bill
from benchmarks.real_apps.fault_proxy import FaultProxy, FaultRule
from benchmarks.real_apps.harness import (
    AppRunResult,
    ReadbackResult,
    RealAppSpec,
    WorkflowSpec,
)

ODOO17_SPEC = RealAppSpec(
    app_id="odoo17",
    display_name="Odoo 17 Community",
    vertical="finance-erp",
    surface="web",
    ui_architecture="owl-spa-jsonrpc",
    persistence_oracle="odoo-jsonrpc+postgres",
    compose_file=Path(__file__).resolve().parent / "docker-compose.yml",
    tier="nightly",
    workflows=(
        WorkflowSpec(
            workflow_id="post_vendor_bill",
            description="Create and post a synthetic vendor bill and verify via JSON-RPC search_read",
            expected_writes=1,
            fault_mode="drop_write_return_200",
        ),
    ),
)


class _InProcessOdooJsonRpcServer:
    """Minimal local Odoo ``/jsonrpc`` server for contract-mode verification."""

    def __init__(self) -> None:
        self.bills: list[dict[str, Any]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        host_str = host.decode("ascii") if isinstance(host, bytes) else str(host)
        return f"http://{host_str}:{port}"

    def __enter__(self) -> _InProcessOdooJsonRpcServer:
        bills = self.bills

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or "0")
                req_doc = json.loads(self.rfile.read(length).decode("utf-8"))
                args = (req_doc.get("params") or {}).get("args") or []
                model = args[3] if len(args) > 3 else ""
                method = args[4] if len(args) > 4 else ""
                method_args = args[5] if len(args) > 5 else []

                result: Any = None
                if model == "account.move" and method == "create":
                    rec = dict(method_args[0])
                    rec["id"] = len(bills) + 1
                    bills.append(rec)
                    result = rec["id"]
                elif model == "account.move" and method == "search_read":
                    domain = method_args[0] if method_args else []
                    ref_filter = next(
                        (cond[2] for cond in domain if cond[0] == "ref"), None
                    )
                    result = [b for b in bills if ref_filter is None or b.get("ref") == ref_filter]

                raw = json.dumps(
                    {"jsonrpc": "2.0", "id": req_doc.get("id", 1), "result": result}
                ).encode("utf-8")
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


def _post_rpc(url: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10.0) as resp:
        return int(resp.status), json.loads(resp.read().decode("utf-8"))


class Odoo17Adapter:
    """RealAppAdapter for Odoo 17."""

    spec = ODOO17_SPEC

    def run_contract_verification(self, work_dir: Path) -> AppRunResult:
        t0 = time.monotonic()
        happy_bill = default_vendor_bill()
        fault_bill = VendorBillFixture(
            ref="BILL-SYN-2026-FAULT",
            partner_name="Synthetic Supply Co.",
            amount_total=890.00,
            currency="USD",
            state="posted",
        )

        with _InProcessOdooJsonRpcServer() as backend, FaultProxy(backend.base_url) as proxy:
            proxy.set_rule(FaultRule(mode="none"))
            _post_rpc(f"{proxy.base_url}/jsonrpc", happy_bill.to_jsonrpc_create_payload())
            happy_rb = readback_odoo_vendor_bill(
                backend.base_url,
                db="odoo_synthetic",
                uid=2,
                credential="odoo_synthetic_pw",
                expected_ref=happy_bill.ref,
                expected_amount=happy_bill.amount_total,
                expected_state=happy_bill.state,
            )

            proxy.set_rule(silent_bill_post_drop_rule())
            fault_status, _ = _post_rpc(
                f"{proxy.base_url}/jsonrpc", fault_bill.to_jsonrpc_create_payload()
            )
            fault_rb = readback_odoo_vendor_bill(
                backend.base_url,
                db="odoo_synthetic",
                uid=2,
                credential="odoo_synthetic_pw",
                expected_ref=fault_bill.ref,
                expected_amount=fault_bill.amount_total,
                expected_state=fault_bill.state,
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
        )

    def run_live_verification(self, work_dir: Path) -> AppRunResult:
        t0 = time.monotonic()
        base_url = os.environ.get("ODOO_BASE_URL", "").strip()
        if not base_url:
            rb = ReadbackResult(
                confirmed=False,
                oracle="odoo17-jsonrpc",
                reason="ODOO_BASE_URL is not set; start docker-compose.yml first",
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
        bill = default_vendor_bill()
        rb = readback_odoo_vendor_bill(
            base_url,
            db=os.environ.get("ODOO_DB_NAME", "odoo_synthetic"),
            uid=int(os.environ.get("ODOO_UID", "2")),
            credential=os.environ.get("ODOO_DB_PW", "odoo_synthetic_pw"),
            expected_ref=bill.ref,
            expected_amount=bill.amount_total,
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
                oracle="odoo17-jsonrpc",
                reason="Live fault verification requires proxy routing",
            ),
        )
