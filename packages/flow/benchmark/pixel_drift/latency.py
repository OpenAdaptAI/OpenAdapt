"""Query latency of the local grounding rung on fixed frames, on any CPU.

``export`` writes frame/crop pairs to a folder: the controls page rendered at
1920x1080 (every control's recorded crop) and the real AVC444 click-step
frames. ``time`` loads ``runtime/local_grounder.py`` from this checkout by
file path, with nothing else from the package, so the same code runs on a
lab machine that has only ``numpy``, ``pillow``, ``onnxruntime``, ``httpx`` and ``opencv-python-headless``. Each pair
is queried ``--repeats`` times warm (the crop's embedding cached, as after a
run's first query) and the p50 and p95 per query are reported::

    python latency.py export OUT_DIR [--avc444 CAMPAIGN_DIR ...]
    python latency.py time OUT_DIR --output results_latency_<host>.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import statistics
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
GROUNDER = HERE.parent.parent / "openadapt_flow" / "runtime" / "local_grounder.py"


def export(out: Path, campaigns: list[Path]) -> None:
    import tempfile

    from playwright.sync_api import sync_playwright

    sys.path.insert(0, str(HERE))
    import run_arms

    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1920, "height": 1080})
            png, boxes = run_arms._render(
                page, run_arms.controls_html(dark=False, toggled=False)
            )
        finally:
            browser.close()
    (out / "controls_1080p.png").write_bytes(png)
    pairs = []
    for cid, (x, y, w, h) in boxes.items():
        region = (int(x) - 4, int(y) - 4, int(w) + 8, int(h) + 8)
        name = f"crop_controls_{cid}.png"
        (out / name).write_bytes(run_arms._crop(png, region))
        pairs.append({"frame": "controls_1080p.png", "crop": name, "set": "controls"})
    with tempfile.TemporaryDirectory() as tmp:
        for i, case in enumerate(run_arms.avc444_cases(campaigns, Path(tmp))):
            frame, crop = f"avc444_{i:03d}.png", f"crop_avc444_{i:03d}.png"
            (out / frame).write_bytes(case["frame"])
            (out / crop).write_bytes(case["template"])
            pairs.append({"frame": frame, "crop": crop, "set": "avc444"})
    (out / "pairs.json").write_text(json.dumps(pairs, indent=1))
    print(f"{len(pairs)} pairs in {out}")


def _load_grounder():
    # local_grounder.py needs only ``Region`` from the package.
    ir = types.ModuleType("openadapt_flow.ir")
    ir.Region = tuple  # type: ignore[attr-defined]
    sys.modules.setdefault("openadapt_flow", types.ModuleType("openadapt_flow"))
    sys.modules["openadapt_flow.ir"] = ir
    spec = importlib.util.spec_from_file_location("local_grounder", GROUNDER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["local_grounder"] = module
    spec.loader.exec_module(module)
    return module


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 1)


def time_pairs(folder: Path, output: Path, repeats: int, threads: int) -> None:
    from PIL import Image

    grounder = _load_grounder()
    model = grounder.LocalGroundingModel.load(threads=threads)
    pairs = json.loads((folder / "pairs.json").read_text())
    per_set: dict[str, list[float]] = {}
    sizes: dict[str, set] = {}
    for pair in pairs:
        frame = (folder / pair["frame"]).read_bytes()
        crop = (folder / pair["crop"]).read_bytes()
        with Image.open(folder / pair["frame"]) as img:
            sizes.setdefault(pair["set"], set()).add(img.size)
        model.locate_anchor(frame, crop)  # cold: embeds the crop once
        for _ in range(repeats):
            started = time.perf_counter()
            model.locate_anchor(frame, crop)
            per_set.setdefault(pair["set"], []).append(
                (time.perf_counter() - started) * 1000.0
            )
    import onnxruntime

    summary = {
        name: {
            "queries": len(values),
            "frame_sizes": sorted(list(s) for s in sizes[name]),
            "p50_ms": _pct(values, 0.5),
            "p95_ms": _pct(values, 0.95),
            "mean_ms": round(statistics.fmean(values), 1),
        }
        for name, values in per_set.items()
    }
    report = {
        "schema": "openadapt.local-grounding-latency.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": {
            "machine": platform.machine(),
            "system": platform.system(),
            "processor": platform.processor(),
            "cpu_count": os.cpu_count(),
            "load_1m": os.getloadavg()[0] if hasattr(os, "getloadavg") else None,
        },
        "onnxruntime": onnxruntime.__version__,
        "provider": "CPUExecutionProvider",
        "threads": threads,
        "repeats": repeats,
        "weights_sha256": grounder.WEIGHTS["sha256"],
        "summary": summary,
    }
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=1))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export")
    ex.add_argument("out", type=Path)
    ex.add_argument("--avc444", nargs="*", type=Path, default=[])
    tm = sub.add_parser("time")
    tm.add_argument("folder", type=Path)
    tm.add_argument("--output", type=Path, required=True)
    tm.add_argument("--repeats", type=int, default=5)
    tm.add_argument("--threads", type=int, default=min(4, os.cpu_count() or 1))
    args = parser.parse_args()
    if args.cmd == "export":
        export(args.out, args.avc444)
    else:
        time_pairs(args.folder, args.output, args.repeats, args.threads)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
