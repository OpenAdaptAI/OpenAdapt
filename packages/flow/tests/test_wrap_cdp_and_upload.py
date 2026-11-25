"""Unit tests for remote CDP (--cdp-url) and --upload support in openadapt wrap."""

from __future__ import annotations

import os
import urllib.request
from pathlib import Path

import pytest

from openadapt_flow.wrap import TaskArgumentsError
from openadapt_flow.wrap import cli as wrap_cli
from openadapt_flow.wrap.process import ENV_CDP_URL, ProcessAgent, child_environment


def test_resolve_cdp_url_precedence():
    env = {
        "OPENADAPT_CDP_URL": "wss://env-cdp.example/ws",
        "BROWSERBASE_CONNECT_URL": "wss://browserbase.example/ws",
    }
    assert (
        wrap_cli._resolve_cdp_url("wss://flag.example/ws", env)
        == "wss://flag.example/ws"
    )
    assert wrap_cli._resolve_cdp_url(None, env) == "wss://env-cdp.example/ws"
    assert (
        wrap_cli._resolve_cdp_url(
            None, {"BROWSERBASE_CONNECT_URL": "wss://browserbase.example/ws"}
        )
        == "wss://browserbase.example/ws"
    )
    assert wrap_cli._resolve_cdp_url(None, {}) is None


def test_resolve_cdp_url_rejects_non_websocket_scheme():
    with pytest.raises(TaskArgumentsError, match="ws:// or wss://"):
        wrap_cli._resolve_cdp_url("https://not-a-ws.example", {})


def test_with_cdp_env_sets_and_restores(monkeypatch):
    monkeypatch.delenv("OPENADAPT_CDP_URL", raising=False)
    with wrap_cli._with_cdp_env("wss://sandbox.example/cdp"):
        assert os.environ["OPENADAPT_CDP_URL"] == "wss://sandbox.example/cdp"
    assert "OPENADAPT_CDP_URL" not in os.environ

    monkeypatch.setenv("OPENADAPT_CDP_URL", "ws://127.0.0.1:9222/old")
    with wrap_cli._with_cdp_env("wss://sandbox.example/new"):
        assert os.environ["OPENADAPT_CDP_URL"] == "wss://sandbox.example/new"
    assert os.environ["OPENADAPT_CDP_URL"] == "ws://127.0.0.1:9222/old"


def test_parser_accepts_cdp_url_and_upload():
    args = wrap_cli.build_parser().parse_args(
        [
            "--task",
            "task.json",
            "--cdp-url",
            "wss://connect.browserbase.com/?apiKey=test",
            "--upload",
        ]
    )
    assert args.cdp_url == "wss://connect.browserbase.com/?apiKey=test"
    assert args.upload is True


def test_process_agent_keeps_cdp_url_while_withholding_other_openadapt_vars():
    agent = ProcessAgent(["echo", "ok"], cdp_url="wss://sandbox.example/cdp")
    assert agent.cdp_url == "wss://sandbox.example/cdp"
    env = child_environment(
        {
            "PATH": "/usr/bin",
            "OPENADAPT_CLOUD_KEY": "secret-cloud-key",
            "OPENADAPT_CDP_URL": "wss://ignored-from-parent",
        }
    )
    assert "OPENADAPT_CLOUD_KEY" not in env
    assert ENV_CDP_URL not in env


def test_resolve_cloud_key_precedence():
    assert (
        wrap_cli._resolve_cloud_key(
            {"OPENADAPT_CLOUD_KEY": "cloud-k", "OPENADAPT_API_KEY": "api-k"}
        )
        == "cloud-k"
    )
    assert wrap_cli._resolve_cloud_key({"OPENADAPT_API_KEY": "api-k"}) == "api-k"
    assert wrap_cli._resolve_cloud_key({}) is None


def test_upload_wrap_receipt_posts_bytes_when_key_present(tmp_path: Path, monkeypatch):
    receipt = tmp_path / "effect-receipt.json"
    receipt.write_text('{"schema":"openadapt.effect-receipt/v1"}', encoding="utf-8")
    sent: list[tuple[str, str, bytes]] = []

    class _Resp:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=15.0):
        sent.append((req.full_url, req.get_header("Authorization"), req.data))
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ok = wrap_cli._upload_wrap_receipt(
        {"receipt": str(receipt)},
        cloud_key="oa_live_test",
        endpoint="https://app.openadapt.ai",
    )
    assert ok is True
    assert sent == [
        (
            "https://app.openadapt.ai/api/v1/receipts",
            "Bearer oa_live_test",
            b'{"schema":"openadapt.effect-receipt/v1"}',
        )
    ]


def test_upload_wrap_receipt_skips_cleanly_without_key_or_receipt(tmp_path: Path):
    receipt = tmp_path / "effect-receipt.json"
    receipt.write_text("{}", encoding="utf-8")
    assert wrap_cli._upload_wrap_receipt({}, cloud_key="oa_live_test") is False
    assert (
        wrap_cli._upload_wrap_receipt({"receipt": str(receipt)}, cloud_key=None)
        is False
    )
