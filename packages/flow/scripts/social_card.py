#!/usr/bin/env python3
"""Render a shareable PNG card from one completed run's local artifacts.

Usage:
    python scripts/social_card.py <run-dir> -o card.png

Reads ``<run-dir>/report.json`` (a typed :class:`openadapt_flow.ir.RunReport`)
plus, when present, ``receipt.json`` and ``bench.json``. Deterministic layout,
no network access, no fonts beyond Pillow's default: the same run directory
always renders byte-identical pixels.

The card states only closed, PHI-free facts the run retained about itself:
the outcome badge, workflow name, duration median, executed trials, the
model-call count (0 on a healthy governed run), short artifact SHA-256 hashes,
and the openadapt.ai URL. No screenshot, parameter, URL, or free-form halt
text is drawn.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

_BG = (16, 18, 22)
_FG = (233, 236, 241)
_MUTED = (140, 148, 160)
_OK = (86, 204, 132)
_WARN = (232, 176, 84)

_WIDTH = 720
_URL = "openadapt.ai"


def _load_report(run_dir: Path):
    from openadapt_flow.ir import RunReport

    report_path = run_dir / "report.json"
    if not report_path.is_file():
        raise SystemExit(f"social_card: {run_dir} holds no report.json")
    return RunReport.model_validate_json(report_path.read_text(encoding="utf-8"))


def _card_stats(run_dir: Path, report) -> dict[str, object]:
    """Closed stats for the card, from the typed report + optional artifacts."""
    executed_ms = [result.elapsed_ms for result in report.results if not result.skipped]
    duration_median = statistics.median(executed_ms) if executed_ms else 0.0

    trials = len([result for result in report.results if not result.skipped])
    bench_median = None
    bench_path = run_dir / "bench.json"
    if bench_path.is_file():
        try:
            bench = json.loads(bench_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            bench = {}
        n = int(bench.get("n") or 0)
        if n > 0:
            trials = n
            bench_median = float(bench.get("total_ms_p50") or 0.0)
    if bench_median is not None:
        duration_median = bench_median

    receipt_digest = ""
    receipt_path = run_dir / "receipt.json"
    if receipt_path.is_file():
        try:
            receipt_digest = str(
                json.loads(receipt_path.read_text(encoding="utf-8")).get(
                    "receipt_digest", ""
                )
            )
        except (OSError, json.JSONDecodeError):
            receipt_digest = ""

    return {
        "outcome": str(
            getattr(report, "execution_outcome", None)
            or ("success" if report.success else "FAILED")
        ),
        "name": str(report.workflow_name),
        "duration_median": duration_median,
        "trials": trials,
        "model_calls": int(report.model_calls),
        "bundle_short": str(report.bundle_content_digest or "")[:12],
        "receipt_short": receipt_digest[:12],
    }


def render_card(stats: dict[str, object]):
    """Draw the card deterministically; returns a PIL Image."""
    from PIL import Image, ImageDraw, ImageFont

    def font(size: int):
        try:
            return ImageFont.load_default(size=size)
        except TypeError:  # pragma: no cover - very old Pillow
            return ImageFont.load_default()

    outcome = str(stats["outcome"])
    accent = _OK if outcome == "VERIFIED" else _WARN
    title_font = font(30)
    big_font = font(20)
    label_font = font(15)
    foot_font = font(13)

    pad = 28
    row_h = 26
    duration_median = float(stats["duration_median"])  # type: ignore[arg-type]
    rows = [
        ("duration median", f"{duration_median:.0f} ms"),
        ("trials", str(stats["trials"])),
        (
            "model calls",
            f"{stats['model_calls']}"
            + (
                " (healthy governed runs make none)" if not stats["model_calls"] else ""
            ),
        ),
        ("bundle sha256", str(stats["bundle_short"]) or "unbound"),
        ("receipt sha256", str(stats["receipt_short"]) or "none issued"),
    ]
    height = pad + 46 + pad + 34 + len(rows) * row_h + pad + 24

    image = Image.new("RGB", (_WIDTH, height), _BG)
    draw = ImageDraw.Draw(image)
    draw.rectangle([(0, 0), (6, height)], fill=accent)

    y = pad
    badge_w = max(120, len(outcome) * 14 + 32)
    draw.rounded_rectangle([(pad, y), (pad + badge_w, y + 38)], radius=8, fill=accent)
    draw.text((pad + 16, y + 7), outcome, font=big_font, fill=_BG)
    y += 52

    name = str(stats["name"])
    if draw.textlength(name, font=title_font) > _WIDTH - 2 * pad:
        while name and draw.textlength(name + "...", font=title_font) > (
            _WIDTH - 2 * pad
        ):
            name = name[:-1]
        name += "..."
    draw.text((pad, y), name, font=title_font, fill=_FG)
    y += 40

    for key, value in rows:
        draw.text((pad, y), key, font=label_font, fill=_MUTED)
        draw.text((pad + 200, y), value, font=label_font, fill=_FG)
        y += row_h

    draw.text(
        (pad, height - pad - 12),
        f"{_URL}  |  deterministic local execution - no screenshots, no "
        "parameters drawn",
        font=foot_font,
        fill=_MUTED,
    )
    return image


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_dir", help="Completed run directory (report.json)")
    parser.add_argument("-o", "--out", default="card.png", help="Output PNG path")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir)
    stats = _card_stats(run_dir, _load_report(run_dir))
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    render_card(stats).save(out_path, format="PNG")
    print(f"Social card written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
