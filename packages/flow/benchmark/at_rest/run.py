"""Measure what encrypting a recording costs per frame and on disk.

Records the same synthetic session twice through the real
:class:`openadapt_flow.recorder.Recorder`, once in plaintext and once with an
encryption key (``docs/phi_at_rest.md``), and reports:

- the one-time key derivation (scrypt) when an encrypted recording starts;
- the time to persist one frame, plaintext ``write_bytes`` against the sealed
  writer (AES-256-GCM plus an atomic rename);
- the time per recorded action end to end (two frames plus one event line);
- the time to append one event line late in a long recording (the sealed log
  is resealed whole on each append);
- bytes on disk for both recordings.

Frames are synthetic 1280x800 screens with table text, so the PNGs compress
like real application screens rather than like a flat colour. Everything is
synthetic; nothing is read from a real application.

    python benchmark/at_rest/run.py --actions 150 --out benchmark/at_rest/results.json
"""

from __future__ import annotations

import argparse
import io
import json
import os
import platform
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from openadapt_flow import crypto, sealed_set
from openadapt_flow.recorder import Recorder

KEY = "benchmark-only-synthetic-key"


def synthetic_screen(index: int, size: tuple[int, int] = (1280, 800)) -> bytes:
    """A deterministic application-like screen: header, sidebar, table rows."""
    image = Image.new("RGB", size, (246, 247, 249))
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, size[0], 56], fill=(32, 64, 120))
    draw.text((20, 18), f"Synthetic Clinic - screen {index}", fill=(255, 255, 255))
    draw.rectangle([0, 56, 220, size[1]], fill=(232, 235, 240))
    for row in range(12):
        draw.text((16, 80 + row * 40), f"Menu item {row}", fill=(40, 40, 40))
    for row in range(24):
        y = 80 + row * 28
        shade = 255 if row % 2 else 240
        draw.rectangle([240, y, size[0] - 20, y + 26], fill=(shade, shade, shade))
        draw.text(
            (250, y + 7),
            f"Testpatient {index:03d}-{row:02d}   DOB 19{row + 40}-0{row % 9 + 1}-1{row % 9}"
            f"   MRN SYN{index * 100 + row:07d}   Follow-up note {row * index}",
            fill=(20, 20, 20),
        )
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


class ScriptedBackend:
    """Hands the recorder a new synthetic screen after every action."""

    def __init__(self, frames: list[bytes]) -> None:
        self._frames = frames
        self._i = 0

    @property
    def viewport(self) -> tuple[int, int]:
        return (1280, 800)

    def screenshot(self) -> bytes:
        return self._frames[self._i % len(self._frames)]

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        self._i += 1

    def type_text(self, text: str) -> None:
        self._i += 1

    def press(self, key: str) -> None:
        self._i += 1

    def scroll(self, dx: int, dy: int) -> None:
        self._i += 1


def _ms(seconds: list[float]) -> dict[str, float]:
    ordered = sorted(seconds)
    return {
        "median_ms": round(statistics.median(ordered) * 1000, 3),
        "p95_ms": round(ordered[int(0.95 * (len(ordered) - 1))] * 1000, 3),
        "mean_ms": round(statistics.fmean(ordered) * 1000, 3),
        "n": len(ordered),
    }


def _disk(root: Path) -> dict[str, int]:
    files = [p for p in root.rglob("*") if p.is_file()]
    return {"files": len(files), "bytes": sum(p.stat().st_size for p in files)}


def record(
    root: Path, frames: list[bytes], actions: int, key: str | None
) -> list[float]:
    recorder = Recorder(
        ScriptedBackend(frames),
        root,
        app_url="http://synthetic.test/",
        settle_interval_s=0.0,
        settle_stable_frames=1,
        encryption_key=key,
    )
    per_action: list[float] = []
    for i in range(actions):
        started = time.perf_counter()
        if i % 3 == 2:
            recorder.type_text(f"Testpatient {i:03d}", param=f"field_{i}")
        else:
            recorder.click(300 + i % 400, 100 + i % 600)
        per_action.append(time.perf_counter() - started)
    recorder.finish()
    return per_action


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--actions", type=int, default=150)
    parser.add_argument("--frames", type=int, default=40, help="distinct screens")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    frames = [synthetic_screen(i) for i in range(args.frames)]
    work = Path(tempfile.mkdtemp(prefix="at-rest-bench-"))
    results: dict[str, Any] = {
        "schema": "openadapt.at-rest-benchmark/v1",
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "cpu_count": os.cpu_count(),
            "load_average_1m": round(os.getloadavg()[0], 2),
        },
        "frame_png_kb": {
            "median": round(statistics.median(len(f) for f in frames) / 1000, 1),
            "min": round(min(len(f) for f in frames) / 1000, 1),
            "max": round(max(len(f) for f in frames) / 1000, 1),
        },
        "actions": args.actions,
    }

    # One-time key derivation when an encrypted recording or sealed set opens.
    derive = []
    for _ in range(5):
        started = time.perf_counter()
        crypto.SetKey.derive(KEY, crypto.SetKey.new_salt())
        derive.append(time.perf_counter() - started)
    results["key_derivation_scrypt"] = _ms(derive)

    # Persisting one frame: plaintext write against the sealed writer.
    plain_dir = work / "frame-plain"
    plain_dir.mkdir()
    plain = []
    for i, frame in enumerate(frames * 5):
        started = time.perf_counter()
        (plain_dir / f"{i:04d}.png").write_bytes(frame)
        plain.append(time.perf_counter() - started)
    writer = sealed_set.SealedSetWriter(work / "frame-sealed", "recording", KEY)
    sealed = []
    for i, frame in enumerate(frames * 5):
        started = time.perf_counter()
        writer.write(f"frames/{i:04d}_before.png", frame)
        sealed.append(time.perf_counter() - started)
    results["persist_one_frame"] = {"plaintext": _ms(plain), "sealed": _ms(sealed)}

    # Whole actions through the real recorder.
    plain_actions = record(work / "rec-plain", frames, args.actions, None)
    sealed_actions = record(work / "rec-sealed", frames, args.actions, KEY)
    results["record_one_action"] = {
        "plaintext": _ms(plain_actions),
        "sealed": _ms(sealed_actions),
        "note": "two frames plus one event line per action; settle wait disabled",
    }
    tail = max(10, args.actions // 10)
    results["record_one_action_last_10pct"] = {
        "plaintext": _ms(plain_actions[-tail:]),
        "sealed": _ms(sealed_actions[-tail:]),
        "note": "the sealed event log is resealed whole on each append",
    }
    # Reading the sealed recording back, as compile does.
    started = time.perf_counter()
    opened = sealed_set.SealedSet.open(work / "rec-sealed", KEY)
    opened.verify_all()
    results["open_and_verify_sealed_recording_ms"] = round(
        (time.perf_counter() - started) * 1000, 1
    )
    # Exact overhead: each sealed file against its own plaintext, plus the
    # header. (The two recordings' totals also differ by which synthetic
    # screens the settle loop happened to capture, so they are informational.)
    sealed_root = work / "rec-sealed"
    plaintext_bytes = sum(len(opened.read_bytes(rel)) for rel in opened.files())
    sealed_disk = _disk(sealed_root)
    results["disk"] = {
        "plaintext_recording": _disk(work / "rec-plain"),
        "sealed_recording": sealed_disk,
        "sealed_plaintext_content_bytes": plaintext_bytes,
        "overhead_bytes": sealed_disk["bytes"] - plaintext_bytes,
        "overhead_percent": round(
            100 * (sealed_disk["bytes"] - plaintext_bytes) / plaintext_bytes, 3
        ),
        "header_bytes": (sealed_root / sealed_set.HEADER).stat().st_size,
        "per_file_overhead_bytes": len(crypto.SET_FILE_MAGIC) + 12 + 16,
    }

    shutil.rmtree(work)
    text = json.dumps(results, indent=2)
    print(text)
    if args.out is not None:
        args.out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
