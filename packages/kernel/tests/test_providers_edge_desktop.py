"""Unit tests for dynamic provider registry, local Edge LoRA runtime, and desktop/Citrix drivers."""

from __future__ import annotations

import importlib
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Tuple

import pytest

from openadapt_kernel.desktop_driver import (
    CitrixBoxGraphDriver,
    DesktopControlAction,
    MacOSAXDriver,
    WindowsUIADriver,
    get_desktop_driver,
)
from openadapt_kernel.edge_lora import EdgeLoRAConfig, EdgeLoRARuntime, LoRAAdapterSpec
from openadapt_kernel.egress_guard import EgressBlockedError
from openadapt_kernel.macro import ControlSignature
from openadapt_kernel.providers import (
    DEFAULT_PROVIDER_REGISTRY,
    OpenAICompatHTTPProvider,
    OptionalDependencyError,
    ProviderRegistry,
    is_loopback_url,
)
from openadapt_kernel.stateful_twin import A11yNodeSpec, TwinRouteSpec


def _start_mock_openai_server() -> Tuple[ThreadingHTTPServer, str, List[Dict[str, Any]]]:
    received: List[Dict[str, Any]] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            received.append(body)
            reply = {
                "id": "chatcmpl-local-1",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": f"model={body.get('model')}|ok",
                        },
                    }
                ],
            }
            raw = json.dumps(reply).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}", received


def test_provider_registry_and_entry_point_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = ProviderRegistry()
    reg.register("llm", "stub_llm", lambda **kw: {"provider": "stub", **kw})
    resolved = reg.resolve("llm", "stub_llm", temperature=0.1)
    assert resolved == {"provider": "stub", "temperature": 0.1}

    class _FakeEP:
        name = "coding_agent:custom_coder"

        def load(self) -> Any:
            return lambda **kw: {"coding_agent": "custom_coder", **kw}

    class _FakeEPs:
        def select(self, *, group: str) -> List[Any]:
            return [_FakeEP()] if group == "openadapt_kernel.providers" else []

    monkeypatch.setattr(importlib.metadata, "entry_points", lambda: _FakeEPs())
    count = reg.discover_entry_points()
    assert count == 1
    assert reg.resolve("coding_agent", "auto", mode="fast") == {
        "coding_agent": "custom_coder",
        "mode": "fast",
    }
    assert any(s.name == "openai_compat_http" for s in DEFAULT_PROVIDER_REGISTRY.list_providers("llm"))


def test_openai_compat_http_egress_guard_and_loopback() -> None:
    assert is_loopback_url("http://127.0.0.1:8000") is True
    assert is_loopback_url("http://localhost:11434/v1") is True
    assert is_loopback_url("https://api.example.com/v1") is False

    # Refuses allow_local_phi=True on non-loopback remote endpoints
    with pytest.raises(ValueError, match="loopback"):
        OpenAICompatHTTPProvider(base_url="https://api.example.com", allow_local_phi=True)

    # Remote endpoint blocks PHI via LocalEgressGuard before any network request is made
    remote_client = OpenAICompatHTTPProvider(base_url="https://api.example.com", model="gpt-test")
    with pytest.raises(EgressBlockedError):
        remote_client.complete([{"role": "user", "content": "Patient JonathanDoe1984 SSN 123-45-6789"}])

    # Loopback endpoint with allow_local_phi=True succeeds against local stub server
    server, base_url, received = _start_mock_openai_server()
    try:
        local_client = OpenAICompatHTTPProvider(
            base_url=base_url, model="local-qwen", allow_local_phi=True
        )
        out = local_client.complete([{"role": "user", "content": "Patient JonathanDoe1984"}])
        assert out == "model=local-qwen|ok"
        assert len(received) == 1
        assert received[0]["model"] == "local-qwen"
    finally:
        server.shutdown()
        server.server_close()


def test_edge_lora_runtime_local_http_loopback_and_peft_dep(monkeypatch: pytest.MonkeyPatch) -> None:
    adapters = (
        LoRAAdapterSpec(name="adapter_pointer_v1", path_or_uri="/opt/loras/pointer", role="pointer_stage"),
        LoRAAdapterSpec(name="adapter_ocr_v1", path_or_uri="/opt/loras/ocr", role="blinded_crop_ocr"),
        LoRAAdapterSpec(name="adapter_probe_v1", path_or_uri="/opt/loras/probe", role="probe_nav"),
    )

    # Loopback enforcement blocks remote URLs
    with pytest.raises(ValueError, match="loopback"):
        EdgeLoRARuntime(
            EdgeLoRAConfig(backend="local_http", base_url="https://remote.example.com", adapters=adapters)
        )

    # local_http routes model=<adapter_name> to local 127.0.0.1 OpenAI-compatible server
    server, base_url, received = _start_mock_openai_server()
    try:
        rt = EdgeLoRARuntime(EdgeLoRAConfig(backend="local_http", base_url=base_url, adapters=adapters))
        reply = rt.infer("blinded_crop_ocr", "Read blinded crop")
        assert reply == "model=adapter_ocr_v1|ok"
        assert rt.active_role == "blinded_crop_ocr"
        assert received[-1]["model"] == "adapter_ocr_v1"
    finally:
        server.shutdown()
        server.server_close()

    # local_peft raises actionable OptionalDependencyError when torch/peft are missing
    real_import = importlib.import_module

    def _missing_peft(name: str, *args: Any, **kwargs: Any) -> Any:
        if name in ("torch", "transformers", "peft"):
            raise ImportError(f"No module named {name!r}", name=name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", _missing_peft)
    peft_rt = EdgeLoRARuntime(
        EdgeLoRAConfig(backend="local_peft", base_model="Qwen/Qwen2.5-3B", adapters=adapters)
    )
    with pytest.raises(OptionalDependencyError, match="pip install torch transformers peft"):
        peft_rt.infer("pointer_stage", "Locate submit button")


def test_citrix_box_graph_driver_and_os_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    nodes = [
        A11yNodeSpec(node_id="fld_code", role="edit", name="Primary Code", value="", bbox=(10, 10, 120, 30)),
        A11yNodeSpec(node_id="btn_save", role="button", name="Save Record", value="", bbox=(10, 50, 90, 30)),
    ]
    transitions = (
        TwinRouteSpec(
            method="POST",
            path_pattern="/submit",
            response_template={"status": "ok"},
            mutates_collection="records",
        ),
    )
    frames = iter([b"\x00" * 64, b"\xff" * 64])
    driver = CitrixBoxGraphDriver(
        nodes=nodes,
        transitions=transitions,
        store_collection="records",
        frame_capture_fn=lambda _clip: next(frames),
    )

    # Capture A11y tree -> project to Zero-PHI StatefulTwinApp
    screen = driver.capture_a11y_tree(screen_id="claim_screen", url_path="/claim")
    assert screen.screen_id == "claim_screen"
    assert len(screen.nodes) == 2

    twin = driver.project_to_zero_phi_twin(screen_id="claim_screen", url_path="/claim")
    assert "/claim" in twin.pages
    assert twin.egress_ledger_path.exists() if hasattr(twin, "egress_ledger_path") else len(twin.pages) >= 1

    # Invoke via ControlSignature + offline read-back + hover delta probe
    res = driver.invoke_control(
        DesktopControlAction(
            action="fill",
            value="J45.909",
            signature=ControlSignature(tag="input", label_tokens=("primary", "code")),
        )
    )
    assert res.ok is True
    assert res.target_node_id == "fld_code"
    assert driver.read_back("fld_code") == "J45.909"
    assert driver.hover_probe(60.0, 25.0) > 0.5

    # OS dispatch checks
    assert isinstance(get_desktop_driver("citrix_box"), CitrixBoxGraphDriver)
    assert isinstance(get_desktop_driver("uia"), WindowsUIADriver)
    assert isinstance(get_desktop_driver("macos_ax"), MacOSAXDriver)

    monkeypatch.setattr(sys, "platform", "linux")
    assert isinstance(get_desktop_driver("auto"), CitrixBoxGraphDriver)
    with pytest.raises(OptionalDependencyError, match="pywinauto"):
        WindowsUIADriver().capture_a11y_tree()
    with pytest.raises(OptionalDependencyError, match="ApplicationServices"):
        MacOSAXDriver().capture_a11y_tree()
