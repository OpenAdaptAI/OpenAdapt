"""Capture frames of the lab desktop over RDP (runs in the xfreerdp container).

Connects with the product's ``XFreeRDPTransport`` and ``FreeRDPBackend``
(1280x800, 32-bit, AVC444 requested) and saves a frame every ``INTERVAL_S``
for ``DURATION_S``. env: OAFLOW_LAB_PROFILE, OAFLOW_<P>_HOST/_USER/_PASSWORD,
OAFLOW_RDP_VIA (relay host:port). Usage: ``rdp_capture.py OUT_DIR``.
"""

import json
import os
import sys
import time
from pathlib import Path

from openadapt_flow.backends.rdp_backend import FreeRDPBackend
from openadapt_flow.backends.xfreerdp import (
    RdpTarget,
    SessionProfile,
    XFreeRDPTransport,
)

INTERVAL_S = 0.4
DURATION_S = float(os.environ.get("OAFLOW_CAPTURE_S", "40"))

p = os.environ.get("OAFLOW_LAB_PROFILE", "winlap").upper()
user = os.environ[f"OAFLOW_{p}_USER"]
host, port = os.environ["OAFLOW_RDP_VIA"].rsplit(":", 1)
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
profile = SessionProfile(
    width=1280, height=800, color_depth=32, lossless=False, avc444=True
)
transport = XFreeRDPTransport(
    RdpTarget(host=host, port=int(port), username=user, ignore_certificate=True),
    password=lambda: os.environ[f"OAFLOW_{p}_PASSWORD"],
    profile=profile,
)
started = time.monotonic()
backend = FreeRDPBackend(transport, max_frame_age_s=5.0)
facts = {"connect_s": round(time.monotonic() - started, 1), "frames": []}
t0 = time.monotonic()
i = 0
while time.monotonic() - t0 < DURATION_S:
    shot = time.monotonic()
    try:
        png = backend.screenshot()
    except Exception as exc:  # a stalled link: record and go on
        facts["frames"].append(
            {"i": i, "t": round(shot - t0, 2), "error": type(exc).__name__}
        )
    else:
        (out / f"frame_{i:04d}.png").write_bytes(png)
        facts["frames"].append(
            {
                "i": i,
                "t": round(shot - t0, 2),
                "ms": round((time.monotonic() - shot) * 1000),
            }
        )
    i += 1
    time.sleep(max(0.0, INTERVAL_S - (time.monotonic() - shot)))
transport.disconnect()
(out / "capture.json").write_text(json.dumps(facts, indent=1))
print(
    json.dumps(
        {k: v for k, v in facts.items() if k != "frames"} | {"n": len(facts["frames"])}
    )
)
