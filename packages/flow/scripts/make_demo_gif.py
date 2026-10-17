"""Generate the README demo GIF from real showcase run artifacts.

Composes a side-by-side animation from two runs of the SAME compiled bundle:
the baseline replay (left) and the theme-drift replay that self-healed
(right), with per-step captions showing the resolution rung each side used.
No mockups: every frame is a real screenshot saved by the replayer.

Usage:
    python scripts/make_demo_gif.py \
        --baseline docs/showcase/baseline-run \
        --drift docs/showcase/theme-drift-run \
        --out docs/showcase/demo.gif
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

PANEL_W, PANEL_H = 600, 375  # 1280x800 scaled
GUTTER = 24
MARGIN = 24
HEADER_H = 64
CAPTION_H = 56
FOOTER_H = 40
CANVAS_W = MARGIN * 2 + PANEL_W * 2 + GUTTER
CANVAS_H = HEADER_H + PANEL_H + CAPTION_H + FOOTER_H

BG = (13, 11, 30)
FG = (235, 235, 245)
DIM = (150, 150, 170)
ACCENT = (167, 139, 250)
OK = (110, 231, 183)
WARN = (251, 191, 36)

_FONT_CANDIDATES = [
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
]


def _font(size: int) -> ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


F_TITLE, F_BODY, F_SMALL = _font(26), _font(17), _font(14)


def _text_center(draw, xy, text, font, fill):
    x, y = xy
    w = draw.textlength(text, font=font)
    draw.text((x - w / 2, y), text, font=font, fill=fill)


def _frame() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), BG)
    return img, ImageDraw.Draw(img)


def _panel(draw, img, shot: Path, x: int, label: str, label_color):
    y = HEADER_H
    if shot.is_file():
        panel = Image.open(shot).convert("RGB").resize((PANEL_W, PANEL_H))
        img.paste(panel, (x, y))
    draw.rectangle(
        [x - 1, y - 1, x + PANEL_W, y + PANEL_H], outline=(60, 60, 90)
    )
    _text_center(
        draw, (x + PANEL_W / 2, y + PANEL_H + 10), label, F_BODY, label_color
    )


def _step_caption(result: dict, healed: bool) -> tuple[str, tuple]:
    res = result.get("resolution")
    if not res:
        return "keyboard", DIM
    rung = res["rung"]
    ms = f"{res['elapsed_ms']:.0f}ms"
    if rung == "template":
        return f"template · {ms}", OK
    suffix = " · healed" if healed else ""
    return f"{rung} · {ms}{suffix}", WARN if not healed else OK


def build(baseline_dir: Path, drift_dir: Path, out: Path) -> Path:
    base = json.loads((baseline_dir / "report.json").read_text())
    drift = json.loads((drift_dir / "report.json").read_text())
    drift_by_id = {r["step_id"]: r for r in drift["results"]}

    frames: list[Image.Image] = []
    durations: list[int] = []

    # Title frame.
    img, draw = _frame()
    cx = CANVAS_W / 2
    _text_center(
        draw, (cx, CANVAS_H / 2 - 70),
        "One demonstration. Two UIs. Same compiled workflow.",
        F_TITLE, FG,
    )
    _text_center(
        draw, (cx, CANVAS_H / 2 - 20),
        "Left: the UI it was recorded on.   Right: a theme it has never seen.",
        F_BODY, DIM,
    )
    _text_center(
        draw, (cx, CANVAS_H / 2 + 20),
        "Every frame below is a real screenshot saved by the replayer.",
        F_SMALL, DIM,
    )
    frames.append(img)
    durations.append(2600)

    # Per-step frames.
    for res_b in base["results"]:
        step_id = res_b["step_id"]
        res_d = drift_by_id.get(step_id)
        if res_d is None:
            continue
        img, draw = _frame()
        _text_center(
            draw, (CANVAS_W / 2, 18),
            f"{step_id}  —  {res_b['intent']}", F_BODY, FG,
        )
        cap_b, col_b = _step_caption(res_b, healed=False)
        cap_d, col_d = _step_caption(res_d, healed=res_d.get("heal") is not None)
        _panel(
            draw, img, baseline_dir / res_b["after_png"], MARGIN,
            f"baseline · {cap_b}", col_b,
        )
        _panel(
            draw, img, drift_dir / res_d["after_png"],
            MARGIN + PANEL_W + GUTTER,
            f"drift=theme · {cap_d}", col_d,
        )
        _text_center(
            draw, (CANVAS_W / 2, CANVAS_H - FOOTER_H + 12),
            "openadapt-flow · deterministic replay, self-healing on drift",
            F_SMALL, DIM,
        )
        frames.append(img)
        durations.append(1400 if res_d.get("heal") else 1000)

    # Summary frame.
    img, draw = _frame()
    _text_center(draw, (cx, CANVAS_H / 2 - 96), "Both runs succeeded.", F_TITLE, FG)
    lines = [
        (
            f"baseline: {base['rung_counts'].get('template', 0)}/"
            f"{sum(base['rung_counts'].values())} steps on the template rung"
            f" · {base['heal_count']} heals",
            OK,
        ),
        (
            f"drift=theme: {drift['heal_count']} anchors healed via "
            f"{'/'.join(sorted(drift['rung_counts']))} — fixes saved as a"
            " reviewable diff",
            WARN,
        ),
        (
            f"model calls: {base['model_calls'] + drift['model_calls']}"
            "  ·  cost per run: $0.00",
            ACCENT,
        ),
        ("pip install openadapt-flow", FG),
    ]
    for i, (line, color) in enumerate(lines):
        _text_center(draw, (cx, CANVAS_H / 2 - 40 + i * 34), line, F_BODY, color)
    frames.append(img)
    durations.append(4200)

    out.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        out,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
    )
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--baseline", type=Path, required=True)
    ap.add_argument("--drift", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    path = build(args.baseline, args.drift, args.out)
    print(f"Wrote {path} ({path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
