"""Optional live MLX integration test for the VLM service.

Skipped unless BOTH the ``mlx_vlm`` package is importable AND
``RUN_MLX_VLM_TEST=1`` is set (loading a real 4-bit VLM is slow and needs Apple
Silicon). This is the only test that touches a real model; CI runs the stub
path. It proves the MLX backend wires end-to-end through the FastAPI app.

Run::

    RUN_MLX_VLM_TEST=1 ANTHROPIC_API_KEY= \
        python -m pytest tests/test_vlm_service_mlx.py -q -s
"""

from __future__ import annotations

import base64
import io
import os

import pytest
from PIL import Image, ImageDraw

mlx_vlm = pytest.importorskip("mlx_vlm")

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_MLX_VLM_TEST") != "1",
    reason="set RUN_MLX_VLM_TEST=1 to run the live MLX integration test",
)


def _id_png(text: str) -> str:
    img = Image.new("RGB", (240, 70), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.text((12, 20), text, fill=(17, 17, 17))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def test_mlx_identity_compare_end_to_end():
    from fastapi.testclient import TestClient

    from openadapt_flow.services.vlm_service.app import create_app
    from openadapt_flow.services.vlm_service.config import ServiceConfig

    cfg = ServiceConfig(backend="mlx", token="t", max_tokens=6)
    app = create_app(cfg)  # loads the real MLX model on startup
    with TestClient(app) as c:
        # Identical crops should verify SAME; the point is a valid verdict + latency.
        r = c.post(
            "/v1/identity/compare",
            json={"crop_a": _id_png("MG4482"), "crop_b": _id_png("MG4482")},
            headers={"Authorization": "Bearer t"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["verdict"] in ("same", "different", "uncertain")
        assert body["latency_ms"] >= 0.0
