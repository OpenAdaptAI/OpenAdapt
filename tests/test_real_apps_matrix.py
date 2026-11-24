"""Comprehensive unit & contract tests for the Real-App Verification Matrix (OpenEMR + Odoo 17)."""

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
                raw = json.dumps({"upstream_received": True, "path": self.path}).encode(
                    "utf-8"
                )
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


def test_registry_exposes_vertical_wave1_apps() -> None:
    specs = list_app_specs()
    ids = [s.app_id for s in specs]
    assert ids == ["openemr", "odoo17"]
    for spec in specs:
        assert get_app_spec(spec.app_id) == spec
        assert spec.workflows
        assert spec.compose_file is not None
        assert spec.compose_file.is_file()

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


@pytest.mark.parametrize("app_id", ["openemr", "odoo17"])
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
    assert doc["execution_target"] == "in-process contract stub"
    assert doc["is_live_app"] is False
    assert doc["total"] == 2
    assert doc["passed"] == 2
    assert [r["app_id"] for r in doc["results"]] == ["openemr", "odoo17"]
    assert all(r["is_live_app"] is False for r in doc["results"])
    assert all(
        r["execution_target"] == "in-process contract stub" for r in doc["results"]
    )

    md = out_md.read_text(encoding="utf-8")
    assert "2/2 in-process contract stubs passed" in md
    assert "NOT live app verification" in md
    assert "in-process contract stub" in md
    assert "OpenEMR 8.0.0.3" in md
    assert "Odoo 17 Community" in md
    assert "OpenAdapt Real-App Contract Matrix" in capsys.readouterr().out

    assert "PASS (stub)" in render_scoreboard_markdown(
        RealAppHarness([load_adapter("openemr")]).run_all(
            mode="contract", work_dir=tmp_path / "single"
        ),
        mode="contract",
    )

    with pytest.raises(NotImplementedError):
        RealAppHarness([load_adapter("openemr")]).run_all(
            mode="live",  # type: ignore[arg-type]
            work_dir=tmp_path / "live",
        )

    wf_text = Path(".github/workflows/real-apps-nightly.yml").read_text(
        encoding="utf-8"
    )
    assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in wf_text
    assert "actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0" in wf_text
    assert "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9" in wf_text
