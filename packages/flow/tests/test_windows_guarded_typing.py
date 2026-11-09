"""Guarded TYPE through the real pinned TLS agent, with synthetic OS edges."""

from __future__ import annotations

import hashlib
import io
import threading
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

import pytest
import requests
from PIL import Image, ImageChops

from openadapt_flow.backend import StructuralResolutionRefused
from openadapt_flow.backends import WindowsBackend
from openadapt_flow.backends.win_agent import (
    AgentConfig,
    create_server,
    generate_self_signed_cert,
)
from openadapt_flow.ir import ActionDeliveryReceipt


def _png(*, changed: bool = False) -> bytes:
    image = Image.new("RGB", (64, 48), "white")
    if changed:
        for y in range(10, 25):
            image.putpixel((26, y), (0, 0, 0))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class _SyntheticDesktop:
    """Only the four OS boundary callbacks are synthetic; HTTP stays real."""

    def __init__(self) -> None:
        self.png = _png()
        self.application = "synthetic-editor"
        self.focused = True
        self.events: list[str] = []
        self.inputs: list[dict] = []
        self.focus_queries: list[dict] = []
        self.responses: list[tuple[str, int]] = []

    def grab(self) -> bytes:
        self.events.append("capture")
        return self.png

    def context(self) -> dict:
        self.events.append("context")
        return {
            "status": "ok",
            "application": self.application,
            "session": "a" * 64,
            "workflow_state": None,
            "application_version": "sha256:" + "b" * 64,
            "environment": "c" * 64,
        }

    def uia(self, operation: str, payload: dict) -> dict:
        assert operation == "focused-at-point"
        self.events.append("focus")
        self.focus_queries.append(payload)
        return {"status": "ok", "focused": self.focused}

    def input(self, payload: dict) -> dict:
        self.events.append("input")
        self.inputs.append(payload)
        return {
            "status": "delivered",
            "receipt_id": "synthetic-typed-delivery",
            "operation": "physical_type_text",
            "native": False,
            "target_fingerprint": None,
            "delivered_at": "2026-11-06T00:00:00+00:00",
            "outcome_verified": False,
        }

    def observe_response(self, response: requests.Response, **kwargs) -> None:
        self.responses.append((urlparse(response.url).path, response.status_code))


@pytest.fixture()
def guarded_type_agent(
    tmp_path: Path,
) -> Iterator[tuple[WindowsBackend, _SyntheticDesktop]]:
    desktop = _SyntheticDesktop()
    cert = generate_self_signed_cert(dirpath=str(tmp_path / "certs"))
    server = create_server(
        AgentConfig(
            host="127.0.0.1", port=0, certfile=cert.certfile, keyfile=cert.keyfile
        ),
        grab_fn=desktop.grab,
        context_fn=desktop.context,
        uia_fn=desktop.uia,
        input_fn=desktop.input,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    session = requests.Session()
    session.trust_env = False
    session.hooks["response"].append(desktop.observe_response)
    host, port = server.server_address[:2]
    backend = WindowsBackend(
        f"https://{host}:{port}",
        viewport=(64, 48),
        timeout_s=3,
        pin_fingerprint=cert.fingerprint,
        require_tls=True,
        session=session,
    )
    thread.start()
    try:
        yield backend, desktop
    finally:
        session.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


def _consume_again_refuses_without_request(
    backend: WindowsBackend, desktop: _SyntheticDesktop, frame_hash: str
) -> None:
    before = (list(desktop.events), list(desktop.responses), list(desktop.inputs))
    with pytest.raises(StructuralResolutionRefused, match="no pre-identity"):
        backend.type_text_guarded("synthetic note", expected_frame_sha256=frame_hash)
    assert (desktop.events, desktop.responses, desktop.inputs) == before


def test_guarded_type_delivers_once_with_a_typed_receipt(guarded_type_agent) -> None:
    backend, desktop = guarded_type_agent
    frame_hash = hashlib.sha256(backend.guarded_keyboard_frame()).hexdigest()
    backend.arm_guarded_keyboard(26, 20)

    receipt = backend.type_text_guarded(
        "synthetic note", expected_frame_sha256=frame_hash
    )

    assert isinstance(receipt, ActionDeliveryReceipt)
    assert receipt.operation == "physical_type_text"
    assert receipt.receipt_id == "synthetic-typed-delivery"
    assert receipt.native is False
    assert receipt.target_fingerprint is None
    assert receipt.outcome_verified is False
    assert desktop.inputs == [
        {"action": "type_text", "text": "synthetic note", "interval_s": 0.05}
    ]
    assert desktop.focus_queries == [{"x": 26, "y": 20}]
    assert desktop.events == [
        "capture",
        "context",
        "capture",
        "context",
        "focus",
        "input",
    ]
    assert desktop.responses == [
        ("/screenshot", 200),
        ("/context/identity", 200),
        ("/input/guarded", 200),
    ]
    _consume_again_refuses_without_request(backend, desktop, frame_hash)


@pytest.mark.parametrize("mutation", ["frame", "context", "focus"])
def test_guarded_type_refuses_drift_before_input_and_consumes_binding(
    guarded_type_agent, mutation: str
) -> None:
    backend, desktop = guarded_type_agent
    original_png = backend.guarded_keyboard_frame()
    frame_hash = hashlib.sha256(original_png).hexdigest()
    backend.arm_guarded_keyboard(26, 20)
    if mutation == "frame":
        desktop.png = _png(changed=True)
        original = Image.open(io.BytesIO(original_png)).convert("RGB")
        changed = Image.open(io.BytesIO(desktop.png)).convert("RGB")
        difference = ImageChops.difference(original, changed)
        assert sum(pixel != (0, 0, 0) for pixel in difference.getdata()) == 15
        assert difference.getbbox() == (26, 10, 27, 25)
    elif mutation == "context":
        desktop.application = "other-synthetic-editor"
    else:
        desktop.focused = False

    with pytest.raises(StructuralResolutionRefused, match=f"stale_{mutation}"):
        backend.type_text_guarded("synthetic note", expected_frame_sha256=frame_hash)

    assert desktop.inputs == []
    expected = ["capture", "context", "capture"]
    if mutation != "frame":
        expected.append("context")
    if mutation == "focus":
        expected.append("focus")
    assert desktop.events == expected
    assert desktop.focus_queries == (
        [{"x": 26, "y": 20}] if mutation == "focus" else []
    )
    assert desktop.responses == [
        ("/screenshot", 200),
        ("/context/identity", 200),
        ("/input/guarded", 409),
    ]
    _consume_again_refuses_without_request(backend, desktop, frame_hash)
