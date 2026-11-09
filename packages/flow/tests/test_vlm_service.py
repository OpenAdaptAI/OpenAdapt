"""Contract, auth, and batching tests for the VLM service.

No GPU / no model: a scripted stub backend stands in for the VLM so CI asserts
the request/response contract, the micro-batching behaviour, and auth rejection.
"""

from __future__ import annotations

import asyncio
import base64
import io

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from openadapt_flow.services.vlm_service.app import create_app
from openadapt_flow.services.vlm_service.backends import StubBackend
from openadapt_flow.services.vlm_service.config import ServiceConfig


def _png(color=(255, 255, 255)) -> str:
    img = Image.new("RGB", (8, 8), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


class _ScriptedBackend(StubBackend):
    """Stub that answers per endpoint by inspecting the prompt keywords."""

    name = "scripted"

    def __init__(
        self,
        *,
        identity="DIFFERENT",
        ground='{"x": null, "y": null}',
        state="UNCERTAIN",
        delay=0.0,
    ):
        super().__init__()
        self._id = identity
        self._ground = ground
        self._state = state
        self._delay = delay

    def generate(self, prompt, images, max_tokens):
        if self._delay:
            import time

            time.sleep(self._delay)
        low = prompt.lower()
        if "same sequence of characters" in low:
            return self._id
        if "pixel coordinates" in low:
            return self._ground
        if "intended" in low:
            return self._state
        return "UNCERTAIN"


def _client(backend=None, token="secret") -> TestClient:
    cfg = ServiceConfig(backend="stub", token=token, window_ms=15, max_batch_size=8)
    app = create_app(cfg, backend=backend or _ScriptedBackend().load())
    return TestClient(app)


AUTH = {"Authorization": "Bearer secret"}


def test_health_and_ready_no_auth():
    with _client() as c:
        assert c.get("/health").json() == {"status": "ok"}
        r = c.get("/ready").json()
        assert r["ready"] is True and r["backend"] == "scripted"


def test_identity_same_and_different():
    with _client(_ScriptedBackend(identity="SAME").load()) as c:
        r = c.post(
            "/v1/identity/compare",
            json={"crop_a": _png(), "crop_b": _png()},
            headers=AUTH,
        )
        assert r.status_code == 200 and r.json()["verdict"] == "same"
    with _client(_ScriptedBackend(identity="DIFFERENT").load()) as c:
        r = c.post(
            "/v1/identity/compare",
            json={"crop_a": _png(), "crop_b": _png()},
            headers=AUTH,
        )
        assert r.json()["verdict"] == "different"


def test_identity_garbled_answer_is_veto():
    # Anything but a clean SAME parses to "different" (veto-only).
    with _client(_ScriptedBackend(identity="hmm not sure").load()) as c:
        r = c.post(
            "/v1/identity/compare",
            json={"crop_a": _png(), "crop_b": _png()},
            headers=AUTH,
        )
        assert r.json()["verdict"] == "different"


def test_ground_point_and_none():
    with _client(_ScriptedBackend(ground='{"x": 100, "y": 200}').load()) as c:
        r = c.post(
            "/v1/ground",
            json={"screenshot": _png(), "target_description": "Save"},
            headers=AUTH,
        )
        assert r.json()["point"] == [100, 200]
    with _client(_ScriptedBackend(ground='{"x": null, "y": null}').load()) as c:
        r = c.post(
            "/v1/ground",
            json={"screenshot": _png(), "target_description": "Save"},
            headers=AUTH,
        )
        assert r.json()["point"] is None


def test_verify_state_yes_no_uncertain():
    for answer, expected in [("YES", "yes"), ("NO", "no"), ("dunno", "uncertain")]:
        with _client(_ScriptedBackend(state=answer).load()) as c:
            r = c.post(
                "/v1/verify_state",
                json={"screenshot": _png(), "expected_state": "saved dialog"},
                headers=AUTH,
            )
            assert r.json()["holds"] == expected


def test_auth_rejected_without_token():
    with _client() as c:
        r = c.post("/v1/identity/compare", json={"crop_a": _png(), "crop_b": _png()})
        assert r.status_code == 401
        r2 = c.post(
            "/v1/identity/compare",
            json={"crop_a": _png(), "crop_b": _png()},
            headers={"Authorization": "Bearer wrong"},
        )
        assert r2.status_code == 401


def test_bad_base64_rejected():
    with _client() as c:
        r = c.post(
            "/v1/ground",
            json={"screenshot": "!!notb64!!", "target_description": "x"},
            headers=AUTH,
        )
        assert r.status_code == 422


def test_micro_batching_groups_concurrent_requests():
    # Fire many concurrent identity requests; a small handler delay guarantees
    # they overlap the batch window, so the batcher must group > 1 together.
    cfg = ServiceConfig(backend="stub", token="secret", window_ms=50, max_batch_size=16)
    backend = _ScriptedBackend(identity="DIFFERENT", delay=0.02).load()
    app = create_app(cfg, backend=backend)

    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=transport, base_url="http://t"
            ) as ac:
                body = {"crop_a": _png(), "crop_b": _png()}
                tasks = [
                    ac.post("/v1/identity/compare", json=body, headers=AUTH)
                    for _ in range(12)
                ]
                resps = await asyncio.gather(*tasks)
            return resps, app.state.batcher.max_observed_batch

    resps, max_batch = asyncio.run(_run())
    assert all(r.status_code == 200 for r in resps)
    assert all(r.json()["verdict"] == "different" for r in resps)
    assert max_batch >= 2, f"expected batching, max batch was {max_batch}"


# -- insecure-exposure startup warnings (empty token / non-loopback bind) -----


def test_no_warning_on_loopback_with_token():
    from openadapt_flow.services.vlm_service.config import insecure_exposure_warnings

    assert insecure_exposure_warnings("127.0.0.1", "secret") == []
    assert insecure_exposure_warnings("localhost", "secret") == []


def test_warns_unauthenticated_phi_on_network():
    from openadapt_flow.services.vlm_service.config import insecure_exposure_warnings

    msgs = insecure_exposure_warnings("0.0.0.0", "")
    assert len(msgs) == 1
    text = msgs[0]
    assert "UNAUTHENTICATED PHI ENDPOINT ON THE NETWORK" in text
    assert "VLM_SERVICE_TOKEN" in text


def test_warns_auth_disabled_on_loopback():
    from openadapt_flow.services.vlm_service.config import insecure_exposure_warnings

    msgs = insecure_exposure_warnings("127.0.0.1", "")
    assert len(msgs) == 1
    assert "AUTH DISABLED" in msgs[0]


def test_warns_cleartext_phi_when_authed_but_non_loopback():
    from openadapt_flow.services.vlm_service.config import insecure_exposure_warnings

    msgs = insecure_exposure_warnings("0.0.0.0", "secret")
    assert len(msgs) == 1
    assert "CLEARTEXT PHI OVER THE NETWORK" in msgs[0]


def test_cli_host_defaults_to_loopback(monkeypatch):
    """The serve entrypoint must not land on the network without explicit --host."""
    from openadapt_flow.services.vlm_service import __main__ as service_main

    captured = {}

    class _FakeUvicorn:
        @staticmethod
        def run(app, host, port):
            captured["host"] = host
            captured["port"] = port

    monkeypatch.setitem(__import__("sys").modules, "uvicorn", _FakeUvicorn)
    monkeypatch.setenv("VLM_BACKEND", "stub")
    service_main.main([])  # no --host
    assert captured["host"] == "127.0.0.1"


def test_cli_warns_when_bound_to_all_interfaces_without_token(monkeypatch, caplog):
    """--host 0.0.0.0 with no token logs the unauthenticated-PHI warning."""
    import logging

    from openadapt_flow.services.vlm_service import __main__ as service_main

    class _FakeUvicorn:
        @staticmethod
        def run(app, host, port):
            pass

    monkeypatch.setitem(__import__("sys").modules, "uvicorn", _FakeUvicorn)
    monkeypatch.setenv("VLM_BACKEND", "stub")
    monkeypatch.delenv("VLM_SERVICE_TOKEN", raising=False)
    with caplog.at_level(logging.WARNING):
        service_main.main(["--host", "0.0.0.0"])
    assert any(
        "UNAUTHENTICATED PHI ENDPOINT ON THE NETWORK" in rec.message
        for rec in caplog.records
    )
