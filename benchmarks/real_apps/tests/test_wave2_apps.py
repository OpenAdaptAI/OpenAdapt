"""Contract and planted-fault verification tests for Wave 2 real-app adapters.

Tests all 6 Wave 2 real-app adapters locally with zero Docker pulls:
- Real local loopback HTTP stub servers for Medplum FHIR, Openkoda, EspoCRM, WooCommerce
- Real stdlib .xlsx and .ods archives for LibreOffice Calc
- Real local SQLite database for Joplin
"""

from __future__ import annotations

import contextlib
import http.server
import json
import threading
import urllib.parse
from pathlib import Path
from typing import Any, Iterator

import pytest

from benchmarks.real_apps.espocrm import (
    fault_injectors as espocrm_faults,
)
from benchmarks.real_apps.espocrm import (
    readback as espocrm_readback,
)
from benchmarks.real_apps.espocrm import (
    seed as espocrm_seed,
)
from benchmarks.real_apps.joplin import (
    fault_injectors as joplin_faults,
)
from benchmarks.real_apps.joplin import (
    readback as joplin_readback,
)
from benchmarks.real_apps.joplin import (
    seed as joplin_seed,
)
from benchmarks.real_apps.libreoffice_calc import (
    fault_injectors as calc_faults,
)
from benchmarks.real_apps.libreoffice_calc import (
    readback as calc_readback,
)
from benchmarks.real_apps.libreoffice_calc import (
    seed as calc_seed,
)
from benchmarks.real_apps.medplum import (
    fault_injectors as medplum_faults,
)
from benchmarks.real_apps.medplum import (
    readback as medplum_readback,
)
from benchmarks.real_apps.medplum import (
    seed as medplum_seed,
)
from benchmarks.real_apps.openkoda import (
    fault_injectors as openkoda_faults,
)
from benchmarks.real_apps.openkoda import (
    readback as openkoda_readback,
)
from benchmarks.real_apps.openkoda import (
    seed as openkoda_seed,
)
from benchmarks.real_apps.woocommerce import (
    fault_injectors as woo_faults,
)
from benchmarks.real_apps.woocommerce import (
    readback as woo_readback,
)
from benchmarks.real_apps.woocommerce import (
    seed as woo_seed,
)

REAL_APPS_DIR = Path(__file__).resolve().parents[1]
WAVE2_APPS = (
    "medplum",
    "openkoda",
    "espocrm",
    "libreoffice_calc",
    "woocommerce",
    "joplin",
)


@contextlib.contextmanager
def _json_http_stub(handler_fn: Any) -> Iterator[str]:
    """Spin up an ephemeral loopback HTTP JSON server for read-back oracle tests."""

    class _Handler(http.server.BaseHTTPRequestHandler):
        def _dispatch(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            body = (
                json.loads(self.rfile.read(length).decode("utf-8")) if length else None
            )
            status, payload = handler_fn(method, self.path, body)
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch("PUT")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("app_id", WAVE2_APPS)
def test_wave2_manifest_and_required_files(app_id: str) -> None:
    app_dir = REAL_APPS_DIR / app_id
    manifest_path = app_dir / "manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["app_id"] == app_id
    assert manifest["display_name"]
    assert manifest["readback_channel"]
    assert len(manifest["fault_classes"]) >= 5
    for py_file in ("seed.py", "readback.py", "fault_injectors.py"):
        assert (app_dir / py_file).is_file()
    if manifest["runtime"] == "docker-compose":
        assert (app_dir / "docker-compose.yml").is_file()
    else:
        assert (app_dir / "desktop_spec.json").is_file()


def test_medplum_fhir_readback_and_all_faults() -> None:
    store: dict[str, dict[str, Any]] = {}
    override_bundle: dict[str, Any] | None = None

    def _handle(method: str, path: str, body: Any) -> tuple[int, Any]:
        if method == "PUT" and path.startswith("/fhir/R4/Patient/"):
            pid = path.rsplit("/", 1)[-1]
            store[pid] = body
            return 200, body
        if method == "GET" and path.startswith("/fhir/R4/Patient?"):
            if override_bundle is not None:
                return 200, override_bundle
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
            mrn = (qs.get("identifier") or [""])[0]
            entries = [
                {"resource": r}
                for r in store.values()
                if any(i.get("value") == mrn for i in r.get("identifier", []))
            ]
            return 200, {
                "resourceType": "Bundle",
                "type": "searchset",
                "total": len(entries),
                "entry": entries,
            }
        return 404, {"error": "not found"}

    with _json_http_stub(_handle) as base_url:
        seeded = medplum_seed.seed_app(base_url)
        assert len(seeded) == 2

        ok = medplum_readback.verify_write(
            base_url,
            "Patient",
            {"identifier": "MRN-88041"},
            {
                "id": "pat-synth-001",
                "name.0.family": "Testwell",
                "birthDate": "1984-03-14",
            },
        )
        assert ok["verdict"] == "VERIFIED"

        clean_bundle = {
            "resourceType": "Bundle",
            "type": "searchset",
            "total": 1,
            "entry": [{"resource": store["pat-synth-001"]}],
        }
        for fault in medplum_faults.SUPPORTED_FAULTS:
            override_bundle = medplum_faults.apply_fault(clean_bundle, fault)
            res = medplum_readback.verify_write(
                base_url,
                "Patient",
                {"identifier": "MRN-88041"},
                {
                    "id": "pat-synth-001",
                    "name.0.family": "Testwell",
                    "birthDate": "1984-03-14",
                },
            )
            assert res["verdict"] == "HALT", f"Medplum fault {fault} was not caught!"


def test_openkoda_rest_readback_and_all_faults() -> None:
    policies: list[dict[str, Any]] = []
    override_records: list[dict[str, Any]] | None = None

    def _handle(method: str, path: str, body: Any) -> tuple[int, Any]:
        if method == "POST" and path == "/api/v2/policies":
            policies.append(body)
            return 201, body
        if method == "GET" and path.startswith("/api/v2/policies?"):
            recs = override_records if override_records is not None else policies
            return 200, {"content": recs}
        return 404, {"error": "not found"}

    with _json_http_stub(_handle) as base_url:
        seeded = openkoda_seed.seed_app(base_url)
        assert len(seeded) == 2
        expected = {
            "insuredName": "Jordan Sample",
            "coverageLimit": 450000,
            "deductible": 1000,
            "status": "QUOTED",
        }

        ok = openkoda_readback.verify_write(
            base_url, "policies", "policyNumber", "POL-HO3-2026-001", expected
        )
        assert ok["verdict"] == "VERIFIED"

        target_list = [policies[0]]
        for fault in openkoda_faults.SUPPORTED_FAULTS:
            override_records = openkoda_faults.apply_fault(target_list, fault)
            res = openkoda_readback.verify_write(
                base_url, "policies", "policyNumber", "POL-HO3-2026-001", expected
            )
            assert res["verdict"] == "HALT", f"Openkoda fault {fault} was not caught!"


def test_espocrm_rest_readback_and_all_faults() -> None:
    accounts: list[dict[str, Any]] = []
    override_payload: dict[str, Any] | None = None

    def _handle(method: str, path: str, body: Any) -> tuple[int, Any]:
        if method == "POST" and path == "/api/v1/Account":
            accounts.append(body)
            return 200, body
        if method == "GET" and path.startswith("/api/v1/Account?"):
            if override_payload is not None:
                return 200, override_payload
            return 200, {"total": len(accounts), "list": accounts}
        return 404, {"error": "not found"}

    with _json_http_stub(_handle) as base_url:
        seeded = espocrm_seed.seed_app(base_url)
        assert len(seeded) == 2
        accounts[0]["stage"] = "Bound"
        expected = {"billingAddressCity": "Toronto", "stage": "Bound"}

        ok = espocrm_readback.verify_write(
            base_url, "Account", "Northstar Home Brokerage", expected
        )
        assert ok["verdict"] == "VERIFIED"

        clean_payload = {"total": 1, "list": [accounts[0]]}
        for fault in espocrm_faults.SUPPORTED_FAULTS:
            override_payload = espocrm_faults.apply_fault(clean_payload, fault)
            res = espocrm_readback.verify_write(
                base_url, "Account", "Northstar Home Brokerage", expected
            )
            assert res["verdict"] == "HALT", f"EspoCRM fault {fault} was not caught!"


def test_woocommerce_order_and_refund_readback_and_all_faults() -> None:
    orders: dict[int, dict[str, Any]] = {}
    state: dict[str, Any] = {}

    def _handle(method: str, path: str, body: Any) -> tuple[int, Any]:
        if method == "POST" and path == "/wp-json/wc/v3/orders":
            orders[int(body["id"])] = body
            return 201, body
        if method == "GET" and path == "/wp-json/wc/v3/orders/1001":
            return 200, state["order"]
        if method == "GET" and path == "/wp-json/wc/v3/orders/1001/refunds":
            return 200, state["refunds"]
        return 404, {"error": "not found"}

    with _json_http_stub(_handle) as base_url:
        seeded = woo_seed.seed_app(base_url)
        assert len(seeded) == 2
        clean_state = {
            "order": {**orders[1001], "status": "refunded"},
            "refunds": [{"id": 5001, "amount": "149.50", "reason": "duplicate charge"}],
        }
        state.update(clean_state)

        ok = woo_readback.verify_write(
            base_url,
            1001,
            {"status": "refunded", "total": "149.50"},
            expected_refund_amount="149.50",
        )
        assert ok["verdict"] == "VERIFIED"

        for fault in woo_faults.SUPPORTED_FAULTS:
            state.clear()
            state.update(woo_faults.apply_fault(clean_state, fault))
            res = woo_readback.verify_write(
                base_url,
                1001,
                {"status": "refunded", "total": "149.50"},
                expected_refund_amount="149.50",
            )
            assert res["verdict"] == "HALT", (
                f"WooCommerce fault {fault} was not caught!"
            )


def test_libreoffice_calc_xlsx_ods_readback_and_all_faults(tmp_path: Path) -> None:
    seeded = calc_seed.seed_app(tmp_path)
    expected_xlsx = {
        "B2": {"value": "1250.00"},
        "B4": {"value": "2000.00", "formula": "=SUM(B2:B3)"},
    }
    expected_ods = {
        "B2": {"value": "1250.00"},
        "B4": {"value": "2000.00", "formula": "=SUM([.B2:.B3])"},
    }
    assert (
        calc_readback.verify_write(seeded["xlsx"], expected_xlsx)["verdict"]
        == "VERIFIED"
    )
    assert (
        calc_readback.verify_write(seeded["ods"], expected_ods)["verdict"] == "VERIFIED"
    )

    for fault in calc_faults.SUPPORTED_FAULTS:
        case_dir = tmp_path / fault
        case_xlsx = calc_seed.seed_app(case_dir)["xlsx"]
        calc_faults.apply_fault(case_xlsx, fault, target_cell="B4")
        res = calc_readback.verify_write(case_xlsx, expected_xlsx)
        assert res["verdict"] == "HALT", (
            f"LibreOffice Calc fault {fault} was not caught!"
        )


def test_joplin_sqlite_readback_and_all_faults(tmp_path: Path) -> None:
    db_path = joplin_seed.seed_app(tmp_path / "clean")
    expected = {
        "parent_id": "folder-ops-001",
        "body": "Verify invoice totals and read back SQLite state.",
    }
    assert (
        joplin_readback.verify_write(db_path, "Q4 Vendor Checklist", expected)[
            "verdict"
        ]
        == "VERIFIED"
    )

    for fault in joplin_faults.SUPPORTED_FAULTS:
        fault_db = joplin_seed.seed_app(tmp_path / fault)
        joplin_faults.apply_fault(fault_db, fault, note_title="Q4 Vendor Checklist")
        res = joplin_readback.verify_write(fault_db, "Q4 Vendor Checklist", expected)
        assert res["verdict"] == "HALT", f"Joplin fault {fault} was not caught!"
