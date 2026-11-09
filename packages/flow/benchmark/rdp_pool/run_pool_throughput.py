"""Throughput of parallel headless FreeRDP sessions on a synthetic target.

For each pool size, starts that many synthetic RDP servers (a virtual display
running ``fixture/ledger_app.py``, served by ``freerdp-shadow-cli3`` on
loopback), connects one :class:`XFreeRDPTransport` per server under one fixed
:class:`SessionProfile`, and looks up accounts through
:class:`FreeRDPBackend`: click the account box, type the number, press Enter,
wait for the result area to change and settle, then OCR it and check the
holder and balance against the generated data.

Synthetic data only. Loopback only. Linux with Xvfb, xfreerdp3,
freerdp-shadow-cli3, xdotool, ImageMagick and python3-tk.

Run:
    python benchmark/rdp_pool/run_pool_throughput.py --sizes 1 2 4 --jobs 40
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import platform
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from PIL import Image

from openadapt_flow.backends.rdp_backend import FreeRDPBackend
from openadapt_flow.backends.xfreerdp import (
    RdpTarget,
    SessionPool,
    SessionProfile,
    XFreeRDPTransport,
    _claim_display,
    _release_display,
)
from openadapt_flow.runtime.identity import upscale_crop

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "fixture"))
from ledger_app import accounts  # noqa: E402

SEED = 1
ENTRY = (300, 52)
RESULT = (40, 115, 900, 100)  # x, y, w, h
PROFILE = SessionProfile(width=1280, height=800, color_depth=24, scale=100)
BASE_PORT = 3600


class Server:
    """One synthetic RDP server: Xvfb + ledger app + shadow server."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.procs: list[subprocess.Popen[bytes]] = []
        self.display: int | None = None

    def start(self) -> None:
        n = _claim_display()
        self.display = n
        env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "DISPLAY": f":{n}"}
        quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        self.procs.append(
            subprocess.Popen(
                ["Xvfb", f":{n}", "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
                env=env,
                **quiet,
            )
        )
        deadline = time.time() + 10
        while not Path(f"/tmp/.X11-unix/X{n}").exists() and time.time() < deadline:
            time.sleep(0.1)
        self.procs.append(
            subprocess.Popen(
                [
                    "/usr/bin/python3",
                    str(HERE / "fixture" / "ledger_app.py"),
                    "--seed",
                    str(SEED),
                ],
                env=env,
                **quiet,
            )
        )
        time.sleep(1.0)
        self.procs.append(
            subprocess.Popen(
                [
                    "freerdp-shadow-cli3",
                    f"/port:{self.port}",
                    "/bind-address:127.0.0.1",
                    "-auth",
                ],
                env=env,
                **quiet,
            )
        )
        time.sleep(1.5)

    def stop(self) -> None:
        for p in reversed(self.procs):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        self.procs.clear()
        _release_display(self.display)


def _region_digest(png: bytes) -> str:
    x, y, w, h = RESULT
    img = Image.open(io.BytesIO(png)).crop((x, y, x + w, y + h))
    return hashlib.sha256(img.tobytes()).hexdigest()


def lookup(
    backend: FreeRDPBackend, acct: str, expected: tuple[str, str]
) -> dict[str, Any]:
    from openadapt_flow.vision.ocr import ocr

    t0 = time.perf_counter()
    before = backend.screenshot()
    start = _region_digest(before)
    backend.click(*ENTRY)
    backend.type_text(acct)
    backend.press("Enter")
    deadline = time.time() + 10
    last = None
    while time.time() < deadline:
        png = backend.screenshot()
        d = _region_digest(png)
        if d != start and d == last:
            break
        last = d
        time.sleep(0.05)
    else:
        raise TimeoutError("result area did not settle")
    # Two reads of the settled frame: native, then a 2x upscale. The job
    # passes if either read shows both expected values. Ground truth is
    # known here because the target is synthetic.
    t1 = time.perf_counter()
    reads = [" ".join(line.text for line in ocr(png, region=RESULT))]
    crop = upscale_crop(png, RESULT)
    if crop is not None:
        reads.append(" ".join(line.text for line in ocr(crop)))
    for i, text in enumerate(reads):
        squashed = "".join(text.split()).lower()
        holder_ok = "".join(expected[0].split()).lower() in squashed
        balance_ok = expected[1] in squashed
        if holder_ok and balance_ok:
            return {
                "read": ["native", "upscaled"][i],
                "act_s": t1 - t0,
                "read_s": time.perf_counter() - t1,
            }
    digits = "".join(c for c in expected[1] if c.isdigit())
    if any(digits in "".join(c for c in r if c.isdigit()) for r in reads):
        # The screen was right; OCR lost the decimal point.
        raise ValueError(f"ocr_decimal_dropped: {reads!r} vs {expected!r}")
    raise ValueError(f"read value did not match: {reads!r} vs {expected!r}")


def run_size(
    size: int, jobs: list[str], data: dict[str, tuple[str, str]]
) -> dict[str, Any]:
    servers = [Server(BASE_PORT + i) for i in range(size)]
    for s in servers:
        s.start()
    sessions = [
        XFreeRDPTransport(
            RdpTarget(
                "127.0.0.1", s.port, username="synthetic", ignore_certificate=True
            ),
            password=lambda: "synthetic",
            profile=PROFILE,
        )
        for s in servers
    ]
    backends: dict[int, FreeRDPBackend] = {}
    try:
        with SessionPool(sessions) as pool:
            for i, sess in enumerate(pool.sessions):
                backends[id(sess)] = FreeRDPBackend(sess, connect=False)
            # Warm up OCR once so model load is not in the timing.
            from openadapt_flow.vision.ocr import ocr

            ocr(backends[id(pool.sessions[0])].screenshot(), region=RESULT)
            run = pool.run(
                jobs, lambda sess, acct: lookup(backends[id(sess)], acct, data[acct])
            )
            out = run.summary()
            out["errors"] = sorted({r.error for r in run.results if r.error})
            out["failures"] = [r.detail for r in run.results if not r.ok]
            oks = [r.value for r in run.results if r.ok and r.value]
            for k in ("act_s", "read_s"):
                vals = sorted(v[k] for v in oks)
                out[f"median_{k}"] = round(vals[len(vals) // 2], 3) if vals else None
            out["ocr_decimal_dropped"] = sum(
                1
                for r in run.results
                if (r.detail or "").startswith("ocr_decimal_dropped")
            )
            out["reads"] = {
                k: sum(
                    1 for r in run.results if r.ok and (r.value or {}).get("read") == k
                )
                for k in ("native", "upscaled")
            }
            out["profile_sha256"] = pool.profile_sha256
            return out
    finally:
        for s in servers:
            s.stop()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sizes", type=int, nargs="+", default=[1, 2, 4])
    ap.add_argument("--jobs", type=int, default=40)
    ap.add_argument("--out", default=str(HERE / "results.json"))
    args = ap.parse_args()
    data = accounts(SEED)
    rng = random.Random(7)
    jobs = rng.sample(sorted(data), args.jobs)
    results = []
    for size in args.sizes:
        r = run_size(size, jobs, data)
        print(json.dumps(r))
        results.append(r)
    Path(args.out).write_text(
        json.dumps(
            {
                "profile": PROFILE.__dict__,
                "jobs_per_size": args.jobs,
                "machine": {
                    "platform": platform.platform(),
                    "cpus": __import__("os").cpu_count(),
                },
                "results": results,
            },
            indent=2,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
