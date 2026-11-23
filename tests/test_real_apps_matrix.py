"""Comprehensive unit & contract tests for Wave 1 of the Real-App Verification Matrix."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from benchmarks.real_apps import (
    FaultProxy,
    FaultRule,
    RealAppHarness,
    get_app_spec,
    list_app_specs,
    load_adapter,
    render_scoreboard_markdown,
)
from benchmarks.real_apps.__main__ import main as cli_main
from benchmarks.real_apps.apps.gnucash.fault_injectors import (
    has_active_lock,
    inject_readonly_lock,
    simulate_unsaved_buffer_write,
)
from benchmarks.real_apps.apps.gnucash.readback import readback_gnucash_transaction
from benchmarks.real_apps.apps.gnucash.seed import (
    LedgerEntryFixture,
    default_ledger_entry,
    record_balanced_transaction,
    seed_gnucash_sqlite,
)
from benchmarks.real_apps.apps.openemr.fault_injectors import (
    session_expiry_redirect_rule,
)


class _EchoUpstream:
    """Minimal real local HTTP upstream for testing all FaultProxy modes."""

    def __init__(self) -> None:
        self.received_posts: list[tuple[str, bytes]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        host_str = host.decode("ascii") if isinstance(host, bytes) else str(host)
        return f"http://{host_str}:{port}"

    def __enter__(self) -> _EchoUpstream:
        posts = self.received_posts

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(length) if length > 0 else b""
                posts.append((self.path, body))
                raw = json.dumps({"upstream_received": True, "path": self.path}).encode("utf-8")
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


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def test_registry_exposes_all_wave1_apps() -> None:
    specs = list_app_specs()
    ids = [s.app_id for s in specs]
    assert ids == ["openemr", "odoo17", "twenty_crm", "gnucash"]
    for spec in specs:
        assert get_app_spec(spec.app_id) == spec
        assert spec.workflows
        if spec.surface == "web":
            assert spec.compose_file is not None
            assert spec.compose_file.is_file()
        else:
            assert spec.compose_file is None

    with pytest.raises(KeyError, match="Unknown app_id"):
        get_app_spec("nonexistent_app")


def test_fault_proxy_all_modes() -> None:
    with _EchoUpstream() as upstream, FaultProxy(upstream.base_url) as proxy:
        # 1. Passthrough
        proxy.set_rule(FaultRule(mode="none"))
        req = urllib.request.Request(
            f"{proxy.base_url}/api/write", data=b"{}", method="POST"
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            assert resp.status == 200
            assert json.loads(resp.read().decode("utf-8"))["upstream_received"] is True
        assert len(upstream.received_posts) == 1

        # 2. drop_write_return_200
        proxy.set_rule(
            FaultRule(mode="drop_write_return_200", path_contains="/api/write")
        )
        req = urllib.request.Request(
            f"{proxy.base_url}/api/write", data=b"{}", method="POST"
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            assert resp.status == 200
            assert json.loads(resp.read().decode("utf-8"))["saved"] is True
        assert len(upstream.received_posts) == 1  # dropped!

        # 3. session_expiry_redirect
        proxy.set_rule(session_expiry_redirect_rule())
        opener = urllib.request.build_opener(_NoRedirectHandler())
        req = urllib.request.Request(
            f"{proxy.base_url}/ServiceRequest", data=b"{}", method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            opener.open(req, timeout=5.0)
        assert exc_info.value.code == 302
        assert "/login" in str(exc_info.value.headers.get("Location"))
        assert len(upstream.received_posts) == 1  # still dropped!

        # 4. optimistic_ui_rollback
        proxy.set_rule(
            FaultRule(mode="optimistic_ui_rollback", path_contains="/graphql")
        )
        req = urllib.request.Request(
            f"{proxy.base_url}/graphql", data=b"{}", method="POST"
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            assert resp.status == 200
            body = json.loads(resp.read().decode("utf-8"))
            assert body["data"]["optimistic"] is True
        assert len(upstream.received_posts) == 1

        # 5. slow_response
        proxy.set_rule(
            FaultRule(mode="slow_response", path_contains="/slow", delay_s=0.05)
        )
        t0 = time.monotonic()
        req = urllib.request.Request(
            f"{proxy.base_url}/slow", data=b"{}", method="POST"
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            assert resp.status == 200
        assert (time.monotonic() - t0) >= 0.04
        assert len(upstream.received_posts) == 2


def test_gnucash_sqlite_seed_readback_and_lock_fault(tmp_path: Path) -> None:
    db_path = seed_gnucash_sqlite(tmp_path / "ledger.gnucash")
    entry = default_ledger_entry()
    record_balanced_transaction(db_path, entry)

    rb = readback_gnucash_transaction(
        db_path,
        expected_tx_guid=entry.tx_guid,
        expected_description=entry.description,
        expected_amount_cents=entry.amount_cents,
    )
    assert rb.confirmed is True
    assert rb.observed_fields["split_count"] == 2
    assert rb.observed_fields["split_sum"] == 0

    assert has_active_lock(db_path) is False
    inject_readonly_lock(db_path)
    assert has_active_lock(db_path) is True

    unsaved = LedgerEntryFixture(
        tx_guid="tx-unsaved-999",
        description="Unsaved entry",
        post_date="2026-11-22 13:00:00",
        amount_cents=5000,
    )
    ui_state = simulate_unsaved_buffer_write(db_path, unsaved)
    assert ui_state["ui_row_visible"] is True
    assert ui_state["reason"] == "readonly_gnclock"

    rb_missing = readback_gnucash_transaction(
        db_path,
        expected_tx_guid=unsaved.tx_guid,
        expected_description=unsaved.description,
        expected_amount_cents=unsaved.amount_cents,
    )
    assert rb_missing.confirmed is False


@pytest.mark.parametrize("app_id", ["openemr", "odoo17", "twenty_crm", "gnucash"])
def test_wave1_app_contract_verification_passes(app_id: str, tmp_path: Path) -> None:
    adapter = load_adapter(app_id)
    harness = RealAppHarness([adapter])
    results = harness.run_all(mode="contract", work_dir=tmp_path)
    assert len(results) == 1
    res = results[0]
    assert res.passed is True
    assert res.happy_path_verified is True
    assert res.silent_fault_caught is True
    assert res.happy_readback.confirmed is True
    assert res.fault_readback.confirmed is False


def test_cli_runner_emits_json_and_markdown_scoreboard(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_json = tmp_path / "scoreboard.json"
    out_md = tmp_path / "scoreboard.md"
    rc = cli_main(
        [
            "run",
            "--app",
            "all",
            "--mode",
            "contract",
            "--work-dir",
            str(tmp_path / "work"),
            "--output-json",
            str(out_json),
            "--output-md",
            str(out_md),
        ]
    )
    assert rc == 0
    assert out_json.is_file()
    assert out_md.is_file()

    doc = json.loads(out_json.read_text(encoding="utf-8"))
    assert doc["schema"] == "openadapt.real-apps-scoreboard/v1"
    assert doc["total"] == 4
    assert doc["passed"] == 4
    assert [r["app_id"] for r in doc["results"]] == [
        "openemr",
        "odoo17",
        "twenty_crm",
        "gnucash",
    ]

    md = out_md.read_text(encoding="utf-8")
    assert "4/4 apps passed" in md
    assert "OpenEMR 8.0.0.3" in md
    assert "Odoo 17 Community" in md
    assert "Twenty CRM" in md
    assert "GnuCash Desktop" in md
    assert "OpenAdapt Real-App Verification Matrix" in capsys.readouterr().out

    # Ensure render_scoreboard_markdown works directly as well.
    assert "PASS" in render_scoreboard_markdown(
        RealAppHarness([load_adapter("gnucash")]).run_all(
            mode="contract", work_dir=tmp_path / "single"
        ),
        mode="contract",
    )
