"""Harness for the real H.264/AVC444 remote-desktop lab. Runs on vm-client.

``python harness.py <job.json> <out_dir>``, started by runner.py inside the
interactive ``harness`` session. Job kinds:

- ``probe``: open the Remote Desktop Connection (mstsc) window for one app,
  capture frames through the shipped ``RemoteDisplayBackend`` +
  ``Win32WindowClient``, and save them with the window geometry.

The backend is the unmodified product code; only the window selection
(process ``mstsc``, exact window title) is configured here.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image

from openadapt_flow.backends.remote_display import RemoteDisplayBackend

APPS = {
    "emr": {"target": "10.30.1.7"},
    "pn": {"target": "vm-rdhost"},
}
DESKTOP = (1280, 800)


def window_title(app: str) -> str:
    return f"{APPS[app]['target']} - Remote Desktop Connection"


def open_session(app: str, *, bpp: int = 32, wait_s: float = 60.0) -> None:
    """Start mstsc for ``app`` unless its window already exists."""
    backend = RemoteDisplayBackend(owner_substr="mstsc", title_substr=window_title(app))
    if backend._client.find_windows("mstsc", window_title(app)):  # noqa: SLF001
        return
    rdp = Path(r"C:\oa") / f"{app}_{bpp}.rdp"
    rdp.write_text(
        "\n".join(
            [
                f"full address:s:{APPS[app]['target']}",
                f"desktopwidth:i:{DESKTOP[0]}",
                f"desktopheight:i:{DESKTOP[1]}",
                "screen mode id:i:1",
                "smart sizing:i:0",
                f"session bpp:i:{bpp}",
                "authentication level:i:0",
                "prompt for credentials:i:0",
                "redirectclipboard:i:0",
                "audiomode:i:2",
                "connection type:i:7",
                "networkautodetect:i:1",
                "bandwidthautodetect:i:1",
                "displayconnectionbar:i:0",
                "autoreconnection enabled:i:1",
            ]
        ),
        encoding="utf-8",
    )
    subprocess.Popen(["mstsc", str(rdp)])
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if backend._client.find_windows("mstsc", window_title(app)):  # noqa: SLF001
            return
        time.sleep(1)
    raise TimeoutError(f"mstsc window for {app} did not appear")


def close_sessions() -> None:
    subprocess.run(["taskkill", "/im", "mstsc.exe", "/f"], capture_output=True)


def backend_for(app: str, **kwargs) -> RemoteDisplayBackend:
    return RemoteDisplayBackend(
        owner_substr="mstsc",
        title_substr=window_title(app),
        max_frame_age_s=kwargs.pop("max_frame_age_s", 10.0),
        **kwargs,
    )


def save_frame(png: bytes, out: Path, name: str) -> dict:
    path = out / f"{name}.png"
    path.write_bytes(png)
    w, h = Image.open(io.BytesIO(png)).size
    return {"file": path.name, "sha256": hashlib.sha256(png).hexdigest(), "size": [w, h]}


def probe(job: dict, out: Path) -> dict:
    app = job["app"]
    open_session(app, bpp=job.get("bpp", 32))
    time.sleep(job.get("settle_s", 20))
    backend = backend_for(app)
    frames = []
    for i in range(job.get("frames", 3)):
        frames.append(save_frame(backend.screenshot(), out, f"{app}_probe_{i}"))
        time.sleep(2)
    windows = [
        {"title": w.title, "bounds": list(w.bounds), "pid": w.pid}
        for w in backend._client.find_windows("mstsc", window_title(app))  # noqa: SLF001
    ]
    return {"app": app, "viewport": list(backend.viewport()), "windows": windows, "frames": frames}


def main(job_path: str, out_dir: str) -> int:
    job = json.loads(Path(job_path).read_text(encoding="utf-8-sig"))
    out = Path(out_dir)
    kinds = {"probe": probe}
    result = kinds[job["kind"]](job, out)
    (out / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
