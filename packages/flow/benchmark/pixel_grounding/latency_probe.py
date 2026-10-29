#!/usr/bin/env python3
"""Per-query latency of the grounding rung at 1080p and 4K captures.

The workflow is recorded once at 1280x800, 100% scaling, as in the sweep.
Each recorded click target is then looked up on three captures of the same
page:

- ``1080p``: 1920x1080 at 100% display scaling;
- ``4k_200``: 3840x2160 at 200% (a 4K monitor at Windows' default scale,
  so the target is drawn at twice its recorded size);
- ``4k_100``: 3840x2160 at 100% (the target at its recorded size in a
  frame four times as large; the costliest case for the edge search).

For each execution provider onnxruntime offers here (CPU always; CoreML,
which can place work on the Apple Neural Engine, when present) it records,
per target, ``REPEATS`` warm queries of ``locate_anchor`` (the recorded
crop's own embedding cached, as after the first query of a run) and one cold
query, and the time spent in each stage: PNG decode, edge search, and the
model on the candidate crops. A last section times the model alone by batch
size and input side, to show what each would save.

Usage::

    python benchmark/pixel_grounding/latency_probe.py --output results_latency.json
"""

from __future__ import annotations

import argparse
import io
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _path in (str(HERE.parents[1]), str(HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import fixture_backend  # noqa: E402
from fixture_backend import Condition, FixtureSession, PixelOnlyBackend  # noqa: E402
from run_sweep import ADA, _git, record  # noqa: E402

TARGETS = ("edit-2", "visit", "note", "tb-attach", "save")
#: Targets the rung is queried for at all. ``edit-2`` is a row anchor with a
#: text label, which the resolver leaves to OCR before calling the model.
QUERIED = ("visit", "note", "tb-attach", "save")
CAPTURES = (
    ("1080p", (1920, 1080), 1.0),
    ("4k_200", (1920, 1080), 2.0),
    ("4k_100", (3840, 2160), 1.0),
)
REPEATS = 30


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))], 1)


def _summary(values: list[float]) -> dict[str, float]:
    return {
        "p50": round(statistics.median(values), 1),
        "p95": _pct(values, 0.95),
        "max": round(max(values), 1),
    }


def _session(provider: str, threads: int):
    import onnxruntime as ort

    from openadapt_flow.runtime.local_grounder import (
        LocalGroundingModel,
        ensure_weights,
    )

    path = ensure_weights(download=True)
    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    providers = (
        [provider]
        if provider == "CPUExecutionProvider"
        else [
            provider,
            "CPUExecutionProvider",
        ]
    )
    session = ort.InferenceSession(str(path), sess_options=options, providers=providers)
    return LocalGroundingModel(session, path)


def _ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=REPEATS)
    parser.add_argument("--threads", type=int, default=min(4, os.cpu_count() or 1))
    args = parser.parse_args()

    import numpy as np
    import onnxruntime as ort
    from PIL import Image
    from playwright.sync_api import sync_playwright

    from openadapt_flow.runtime import local_grounder as lg

    providers = [
        p
        for p in ("CPUExecutionProvider", "CoreMLExecutionProvider")
        if p in ort.get_available_providers()
    ]
    rows = []
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
        browser = pw.chromium.launch()
        workflow, bundle_dir = record(browser, Path(tmp))
        clicks = [s for s in workflow.steps if s.anchor is not None]
        assert len(clicks) == len(TARGETS)
        templates = {
            target: (bundle_dir / step.anchor.template).read_bytes()
            for step, target in zip(clicks, TARGETS)
        }
        frames = {}
        for name, css, scale in CAPTURES:
            fixture_backend.CSS_VIEWPORT = css
            session = FixtureSession(browser, Condition(name, "latency", scale=scale))
            session.select_patient(ADA)
            frames[name] = PixelOnlyBackend(session).screenshot()
            session.close()
        browser.close()

    for provider in providers:
        model = _session(provider, args.threads)
        # One throwaway query so session start-up is not counted.
        model.locate_anchor(frames["1080p"], templates["save"])
        for capture, png in frames.items():
            size = Image.open(io.BytesIO(png)).size
            for target in TARGETS:
                template_png = templates[target]
                model._template_cache.clear()
                started = time.perf_counter()
                found = model.locate_anchor(png, template_png)
                cold = _ms(started)
                total, decode, edges, embed, n_candidates = [], [], [], [], []
                for _ in range(args.repeats):
                    started = time.perf_counter()
                    model.locate_anchor(png, template_png)
                    total.append(_ms(started))
                    # The same work, stage by stage.
                    started = time.perf_counter()
                    frame = Image.open(io.BytesIO(png)).convert("RGB")
                    template = Image.open(io.BytesIO(template_png)).convert("RGB")
                    decode.append(_ms(started))
                    started = time.perf_counter()
                    proposals = lg.edge_candidates(frame, template)
                    edges.append(_ms(started))
                    n_candidates.append(len(proposals))
                    crops = [
                        frame.crop((x, y, x + w, y + h))
                        for _s, (x, y, w, h), _sc in proposals
                    ]
                    started = time.perf_counter()
                    if crops:
                        model._embed(crops)
                    embed.append(_ms(started))
                row = {
                    "provider": provider,
                    "capture": capture,
                    "frame": list(size),
                    "target": target,
                    "queried_in_replay": target in QUERIED,
                    "lookalikes": len(found),
                    "candidates": int(statistics.median(n_candidates)),
                    "cold_ms": round(cold, 1),
                    "warm_ms": _summary(total),
                    "decode_ms": _summary(decode),
                    "edge_search_ms": _summary(edges),
                    "model_ms": _summary(embed),
                }
                rows.append(row)
                print(json.dumps(row), flush=True)

    # The model alone, by batch size and input side, on CPU.
    model = _session("CPUExecutionProvider", args.threads)
    rng = np.random.default_rng(0)
    crop = Image.fromarray(rng.integers(0, 255, (40, 40, 3), dtype=np.uint8))
    cells = [(side, n) for side in (112, 84, 56) for n in (1, 4, 8, 12)]
    times: dict[tuple[int, int], list[float]] = {cell: [] for cell in cells}
    errors: dict[tuple[int, int], str] = {}
    # Rounds visit every cell in turn, so a burst of load from other jobs
    # lands on all cells alike instead of on whichever ran last.
    for _ in range(args.repeats + 1):
        for side, n in cells:
            if (side, n) in errors:
                continue
            lg.INPUT_SIDE = side
            try:
                started = time.perf_counter()
                model._embed([crop] * n)
                times[(side, n)].append(_ms(started))
            except Exception as exc:  # an export without dynamic input size
                errors[(side, n)] = str(exc)
    lg.INPUT_SIDE = 112
    batches = []
    for side, n in cells:
        if (side, n) in errors:
            batches.append({"input_side": side, "batch": n, "error": errors[(side, n)]})
        else:
            # The first round warms each input size up and is not counted.
            summary = _summary(times[(side, n)][1:])
            batches.append({"input_side": side, "batch": n, **summary})
        print(json.dumps(batches[-1]), flush=True)

    payload = {
        "schema": "openadapt.pixel-grounding-latency.v1",
        "commit": _git("rev-parse", "HEAD"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "platform": (
            f"{platform.system()} {platform.machine()} "
            f"py{platform.python_version()} onnxruntime {ort.__version__}"
        ),
        "threads": args.threads,
        "repeats": args.repeats,
        "load_average": list(os.getloadavg()),
        "providers": providers,
        "rows": rows,
        "model_by_batch": batches,
    }
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
