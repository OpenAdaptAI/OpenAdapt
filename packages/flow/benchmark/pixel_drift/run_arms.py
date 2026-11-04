"""Screen-only grounding under drift: correct, wrong and refused, per suite.

Runs the full resolution ladder (``runtime.resolver.resolve``) with no
structural backend, as on Citrix, RDP or VNC, against three suites (see
``PREREGISTRATION.md``):

- ``drift21``: the 21-button page of ``benchmark/structural_action``;
- ``controls``: icon-only buttons, checkboxes and radio buttons, rendered
  in Chromium under six conditions;
- ``avc444``: real frames from the H.264/AVC444 RDP campaigns behind
  ``benchmark/rdp_avc_real`` (their run archives are not in the repository;
  pass the campaign folders).

A frame drawn at another display scale is handled as the replayer handles
an RDP or Citrix bundle: the session's scale is estimated from the anchors'
landmark labels and the frame is resampled into the recorded geometry; with no
estimate, the layout-drift check stops the step (counted as refused).

Each target is ``correct`` (inside the true box), ``wrong`` (anywhere else),
``refused`` (a typed refusal) or ``miss`` (no rung resolved)::

    python run_arms.py --arm C --output results_C.json [--local-grounding] \\
        [--avc444 CAMPAIGN_DIR ...]
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import platform
import statistics
import subprocess
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from PIL import Image

HERE = Path(__file__).resolve().parent

# -- controls page -----------------------------------------------------------

# Eight distinct glyphs drawn from plain SVG shapes, so no icon font or icon
# set licence is involved.
GLYPHS = {
    "circle": '<circle cx="10" cy="10" r="6" fill="none" stroke="C" stroke-width="2"/>',
    "square": '<rect x="4" y="4" width="12" height="12" fill="C"/>',
    "triangle": '<path d="M10 3 L17 16 L3 16 Z" fill="C"/>',
    "plus": '<path d="M10 3 V17 M3 10 H17" stroke="C" stroke-width="3"/>',
    "cross": '<path d="M4 4 L16 16 M16 4 L4 16" stroke="C" stroke-width="3"/>',
    "bars": '<path d="M3 5 H17 M3 10 H17 M3 15 H17" stroke="C" stroke-width="2"/>',
    "arrow": '<path d="M3 10 H15 M10 5 L15 10 L10 15" fill="none" stroke="C" stroke-width="2"/>',
    "dots": '<circle cx="5" cy="10" r="2" fill="C"/><circle cx="10" cy="10" r="2" fill="C"/><circle cx="15" cy="10" r="2" fill="C"/>',
}
PENCIL = '<path d="M4 16 L5 12 L13 4 L16 7 L8 15 Z" fill="none" stroke="C" stroke-width="2"/>'
CHECKS = ["Allergies", "Smoker", "Diabetic", "Insured"]
RADIOS = ["Morning", "Afternoon", "Evening"]
ROWS = ["Row Alpha", "Row Bravo"]
CONDITIONS = ("unchanged", "scale125", "scale150", "dark", "jpeg50", "toggled")


def _svg(body: str, colour: str) -> str:
    return (
        '<svg width="20" height="20" viewBox="0 0 20 20">'
        + body.replace('"C"', f'"{colour}"')
        + "</svg>"
    )


def controls_html(*, dark: bool, toggled: bool) -> str:
    fg = "#dde3ea" if dark else "#23303d"
    bg = "#1c2228" if dark else "#f4f6f8"
    tools = "".join(
        f'<button class="ic" id="tool-{name}" title="{name}" '
        f'style="left:{40 + i * 44}px;top:30px">{_svg(body, fg)}</button>'
        for i, (name, body) in enumerate(GLYPHS.items())
    )
    rows = "".join(
        f'<div class="lbl" style="left:40px;top:{110 + i * 44}px">{label}</div>'
        f'<button class="ic" id="row-{i}" style="left:200px;top:{104 + i * 44}px">'
        f"{_svg(PENCIL, fg)}</button>"
        for i, label in enumerate(ROWS)
    )
    checks = "".join(
        f'<label class="opt" style="left:40px;top:{230 + i * 30}px">'
        f'<input type="checkbox" id="chk-{i}"{" checked" if (i % 2 == 0) != toggled else ""}>'
        f"{label}</label>"
        for i, label in enumerate(CHECKS)
    )
    radios = "".join(
        f'<label class="opt" style="left:300px;top:{230 + i * 30}px">'
        f'<input type="radio" name="slot" id="rad-{i}"{" checked" if i == (1 if toggled else 0) else ""}>'
        f"{label}</label>"
        for i, label in enumerate(RADIOS)
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        f"html,body{{margin:0;background:{bg};color:{fg};"
        "font-family:Arial,Helvetica,sans-serif;font-size:15px;"
        f"color-scheme:{'dark' if dark else 'light'}}}"
        ".ic{position:absolute;width:32px;height:32px;padding:5px;"
        f"border:1px solid {'#56616d' if dark else '#9aa5b1'};border-radius:4px;"
        f"background:{'#2a323a' if dark else '#ffffff'}}}"
        ".lbl,.opt{position:absolute}"
        ".opt input{margin:0 8px 0 0;width:15px;height:15px;vertical-align:-2px}"
        "</style></head><body>"
        f"{tools}{rows}{checks}{radios}</body></html>"
    )


def _control_ids() -> list[str]:
    return (
        [f"tool-{n}" for n in GLYPHS]
        + [f"row-{i}" for i in range(len(ROWS))]
        + [f"chk-{i}" for i in range(len(CHECKS))]
        + [f"rad-{i}" for i in range(len(RADIOS))]
    )


def _landmark_text(control_id: str) -> Optional[str]:
    kind, _, index = control_id.partition("-")
    if kind == "row":
        return ROWS[int(index)]
    if kind == "chk":
        return CHECKS[int(index)]
    if kind == "rad":
        return RADIOS[int(index)]
    return None


def _render(page: Any, html: str) -> tuple[bytes, dict[str, tuple]]:
    """Screenshot the page and every control's box."""
    page.set_content(html)
    page.wait_for_timeout(80)
    png = page.screenshot()
    boxes = {}
    for cid in _control_ids():
        b = page.locator(f"#{cid}").bounding_box()
        boxes[cid] = (b["x"], b["y"], b["width"], b["height"])
    return png, boxes


def _jpeg(png: bytes, quality: int = 50) -> bytes:
    with Image.open(io.BytesIO(png)) as img:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=quality, subsampling=2)
    with Image.open(io.BytesIO(buf.getvalue())) as img:
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()


def _crop(png: bytes, region: tuple[int, int, int, int]) -> bytes:
    x, y, w, h = region
    with Image.open(io.BytesIO(png)) as img:
        buf = io.BytesIO()
        img.crop((x, y, x + w, y + h)).save(buf, format="PNG")
        return buf.getvalue()


def controls_cases() -> list[dict]:
    """Record every control on the unchanged page; replay under each condition."""
    from playwright.sync_api import sync_playwright

    from openadapt_flow import vision
    from openadapt_flow.ir import Anchor, Landmark

    cases = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            base_png, base = _render(page, controls_html(dark=False, toggled=False))
            anchors, templates = {}, {}
            for cid, (x, y, w, h) in base.items():
                region = (int(x) - 4, int(y) - 4, int(w) + 8, int(h) + 8)
                cx, cy = int(x + w / 2), int(y + h / 2)
                landmarks = []
                if _landmark_text(cid) is not None:
                    # The compiler takes a landmark's place from OCR on the
                    # recorded frame, not from the page.
                    found = vision.find_text(base_png, _landmark_text(cid))
                    assert found is not None, cid
                    lcx, lcy = found.point
                    landmarks.append(
                        Landmark(
                            relation="left_of" if lcx < cx else "right_of",
                            ocr_text=_landmark_text(cid),
                            distance_px=int(abs(lcx - cx)),
                            dx_px=int(round(cx - lcx)),
                            dy_px=int(round(cy - lcy)),
                        )
                    )
                anchors[cid] = Anchor(
                    template=f"templates/{cid}.png",
                    region=region,
                    click_point=(cx, cy),
                    ocr_text=None,
                    landmarks=landmarks,
                )
                templates[cid] = _crop(base_png, region)
            page.close()
            for condition in CONDITIONS:
                scale = {"scale125": 1.25, "scale150": 1.5}.get(condition, 1.0)
                ctx = browser.new_context(
                    viewport={"width": 1280, "height": 800}, device_scale_factor=scale
                )
                page = ctx.new_page()
                png, live = _render(
                    page,
                    controls_html(
                        dark=condition == "dark", toggled=condition == "toggled"
                    ),
                )
                ctx.close()
                if condition == "jpeg50":
                    png = _jpeg(png)
                for cid in _control_ids():
                    x, y, w, h = live[cid]
                    truth = tuple(v * scale for v in (x - 3, y - 3, w + 6, h + 6))
                    cases.append(
                        {
                            "suite": "controls",
                            "condition": condition,
                            "target": cid,
                            "recorded_viewport": (1280, 800),
                            "all_anchors": list(anchors.values()),
                            "anchor": anchors[cid],
                            "template": templates[cid],
                            "frame": png,
                            "truth_box": truth,
                        }
                    )
        finally:
            browser.close()
    return cases


def drift21_cases() -> list[dict]:
    from playwright.sync_api import sync_playwright

    from openadapt_flow.ir import Anchor
    from openadapt_flow.validation import structural_action as sa

    n = 21
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.set_content(sa.build_html(n, drift=False))
            page.wait_for_timeout(60)
            base_png = page.screenshot()
            anchors = []
            for i in range(n):
                b = page.locator(f"#open-p{i}").bounding_box()
                x, y, w, h = b["x"], b["y"], b["width"], b["height"]
                anchors.append(
                    Anchor(
                        template=f"templates/open-p{i}.png",
                        region=(int(x) - 6, int(y) - 6, int(w) + 12, int(h) + 12),
                        click_point=(int(x + w / 2), int(y + h / 2)),
                        ocr_text="Open",
                    )
                )
            page.set_content(sa.build_html(n, drift=True))
            page.wait_for_timeout(60)
            png = page.screenshot()
            cases = []
            for i in range(n):
                b = page.locator(f"#open-p{i}").bounding_box()
                cases.append(
                    {
                        "suite": "drift21",
                        "condition": "unchanged" if sa._is_stable(i, n) else "drifted",
                        "target": f"open-p{i}",
                        "anchor": anchors[i],
                        "template": _crop(base_png, anchors[i].region),
                        "frame": png,
                        "truth_box": (b["x"], b["y"], b["width"], b["height"]),
                    }
                )
        finally:
            browser.close()
    return cases


def avc444_cases(campaigns: list[Path], tmp: Path) -> list[dict]:
    """Click steps on real AVC444 frames, with ``rdp_avc_real``'s ground truth."""
    spec = importlib.util.spec_from_file_location(
        "grounding_heldout", HERE.parent / "rdp_avc_real" / "grounding_heldout.py"
    )
    assert spec and spec.loader
    gh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gh)
    cases = []
    for campaign in campaigns:
        trials = json.loads((campaign / "trials.json").read_text())
        app = "pn" if (campaign / "pn_bundle.zip").exists() else "emr"
        workflow, bundle_dir = gh._load_bundle(campaign, app, tmp)
        clicks = {s.id: s for s in workflow.steps if s.anchor is not None}
        for run_zip in sorted(campaign.glob(f"{app}_run_*.zip")):
            condition = run_zip.stem.split("_run_", 1)[1]
            for suffix in ("_false_stop", "_stop_on_lookalike", "_stopped_after_save"):
                condition = condition.removesuffix(suffix)
            verified = gh._verified_points(campaign, trials, condition)
            with zipfile.ZipFile(run_zip) as zf:
                names = set(zf.namelist())
                for step_id, step in clicks.items():
                    name = f"steps/{step_id}_before.png"
                    if name not in names:
                        continue
                    anchor = step.anchor
                    if step_id in verified:
                        truth = verified[step_id]
                    elif condition in gh.LAYOUT_SAME:
                        x, y = anchor.click_point
                        if app == "pn" and abs(y - 145) <= 6:
                            y += gh.PN_ROW_PITCH * gh.PN_ROWS_ABOVE.get(condition, 0)
                        truth = (x, y)
                    else:
                        continue
                    tx, ty = gh.TOL_X, gh.TOL_Y
                    cases.append(
                        {
                            "suite": "avc444",
                            "condition": f"{app}/{condition}",
                            "target": f"{campaign.name}/{run_zip.name}/{step_id}",
                            "anchor": anchor,
                            "template": (bundle_dir / anchor.template).read_bytes(),
                            "frame": zf.read(name),
                            "truth_box": (truth[0] - tx, truth[1] - ty, 2 * tx, 2 * ty),
                        }
                    )
    return cases


# -- live RDP page -----------------------------------------------------------

LIVE_STATES = ("unchanged", "toggled", "dark")
#: Fiducials, in CSS px: two magenta squares fix the frame's offset and scale;
#: a third square's colour names the state the page is showing.
FIDUCIALS = ((600, 700), (1100, 700))
STATE_MARK = (900, 700)
STATE_COLOURS = {"unchanged": "#ff0000", "toggled": "#00ff00", "dark": "#0000ff"}
FIDUCIAL_SIZE = 12


def live_pages(seconds: float = 6.0) -> dict[str, str]:
    """The controls page in each state; each one moves on to the next after
    ``seconds``, with a running counter so the encoder keeps sending."""
    pages = {}
    for index, state in enumerate(LIVE_STATES):
        html = controls_html(dark=state == "dark", toggled=state == "toggled")
        marks = "".join(
            f'<div style="position:absolute;left:{x}px;top:{y}px;'
            f'width:{FIDUCIAL_SIZE}px;height:{FIDUCIAL_SIZE}px;background:#ff00ff"></div>'
            for x, y in FIDUCIALS
        ) + (
            f'<div style="position:absolute;left:{STATE_MARK[0]}px;top:{STATE_MARK[1]}px;'
            f"width:{FIDUCIAL_SIZE}px;height:{FIDUCIAL_SIZE}px;"
            f'background:{STATE_COLOURS[state]}"></div>'
            '<div id="tick" style="position:absolute;left:700px;top:600px">0</div>'
            "<script>let n=0;setInterval(()=>{document.getElementById('tick')"
            ".textContent=String(++n)},100);setTimeout(()=>location.replace("
            f"'{LIVE_STATES[(index + 1) % len(LIVE_STATES)]}.html'),{int(seconds * 1000)});"
            "</script>"
        )
        pages[state] = html.replace("</body>", marks + "</body>")
    return pages


def _frame_state(png: bytes) -> Optional[str]:
    """The state the live page shows, from the colour of its state square."""
    with Image.open(io.BytesIO(png)) as img:
        r, g, b = img.convert("RGB").getpixel(
            (STATE_MARK[0] + FIDUCIAL_SIZE // 2, STATE_MARK[1] + FIDUCIAL_SIZE // 2)
        )
    channels = {"unchanged": r, "toggled": g, "dark": b}
    state = max(channels, key=channels.get)
    others = sorted(channels.values())[:2]
    return state if channels[state] > 180 and max(others) < 140 else None


def _fiducial_offset(png: bytes) -> tuple[float, float]:
    """Where the first magenta fiducial is, against where the page put it."""
    import numpy as np

    with Image.open(io.BytesIO(png)) as img:
        rgb = np.asarray(img.convert("RGB")).astype(int)
    x0, y0 = FIDUCIALS[0]
    win = rgb[y0 - 20 : y0 + 32, x0 - 20 : x0 + 32]
    mask = (win[..., 0] > 180) & (win[..., 1] < 100) & (win[..., 2] > 180)
    ys, xs = np.nonzero(mask)
    assert len(xs) > 20, "fiducial not found"
    return (
        float(xs.min()) - 20.0,
        float(ys.min()) - 20.0,
    )


def rdp_live_cases(runs: list[Path], every: int = 5) -> list[dict]:
    """Frames of the live page captured over RDP (``rdp_capture.py``).

    The first run's first ``unchanged`` frame is the recording: every
    control's crop and its label landmark are taken from it. Every run is
    then scored on every ``every``-th frame and on the two frames after each
    page change, whatever state the page shows: the controls never move, so
    the true boxes are the page's boxes shifted by the fiducial's offset.
    """
    from playwright.sync_api import sync_playwright

    from openadapt_flow import vision
    from openadapt_flow.ir import Anchor, Landmark

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            _png, css = _render(page, live_pages()["unchanged"])
        finally:
            browser.close()
    frames = {run: sorted(run.glob("frame_*.png")) for run in runs}
    record_png = next(
        f.read_bytes()
        for f in frames[runs[0]]
        if _frame_state(f.read_bytes()) == "unchanged"
    )
    ox, oy = _fiducial_offset(record_png)
    anchors, templates = {}, {}
    for cid, (x, y, w, h) in css.items():
        x, y = x + ox, y + oy
        region = (int(x) - 4, int(y) - 4, int(w) + 8, int(h) + 8)
        cx, cy = int(x + w / 2), int(y + h / 2)
        landmarks = []
        if _landmark_text(cid) is not None:
            found = vision.find_text(record_png, _landmark_text(cid))
            assert found is not None, cid
            lcx, lcy = found.point
            landmarks.append(
                Landmark(
                    relation="left_of" if lcx < cx else "right_of",
                    ocr_text=_landmark_text(cid),
                    distance_px=int(abs(lcx - cx)),
                    dx_px=int(round(cx - lcx)),
                    dy_px=int(round(cy - lcy)),
                )
            )
        anchors[cid] = Anchor(
            template=f"templates/{cid}.png",
            region=region,
            click_point=(cx, cy),
            ocr_text=None,
            landmarks=landmarks,
        )
        templates[cid] = _crop(record_png, region)
    cases = []
    for run in runs:
        states = [_frame_state(f.read_bytes()) for f in frames[run]]
        picked = set(range(0, len(states), every))
        for i in range(1, len(states)):
            if states[i] != states[i - 1]:
                picked.update({i, i + 1})
        for i in sorted(picked):
            if i >= len(states) or states[i] is None:
                continue
            png = frames[run][i].read_bytes()
            fx, fy = _fiducial_offset(png)
            for cid, (x, y, w, h) in css.items():
                cases.append(
                    {
                        "suite": "rdp_live",
                        "condition": f"{run.name}/{states[i]}",
                        "target": f"{frames[run][i].name}/{cid}",
                        "anchor": anchors[cid],
                        "template": templates[cid],
                        "frame": png,
                        "truth_box": (x + fx - 3, y + fy - 3, w + 6, h + 6),
                    }
                )
    return cases


# -- scoring -----------------------------------------------------------------


def _inside(point, box) -> bool:
    x, y, w, h = box
    return x <= point[0] <= x + w and y <= point[1] <= y + h


class _LayoutDrift(Exception):
    """The frame is at another scale and no render transform was found."""


def _render_transform(case: dict, vision: Any) -> Any:
    """The session's scale, estimated as the replayer does for RDP and
    Citrix bundles: anchor landmark labels matched on the live frame."""
    from types import SimpleNamespace

    from openadapt_flow.runtime import render_scale
    from openadapt_flow.runtime.resolver import png_size

    steps = [SimpleNamespace(anchor=a) for a in case["all_anchors"]]
    pairs = render_scale.match_landmarks(
        render_scale.anchor_landmarks(steps), vision.ocr(case["frame"])
    )
    transform, _reason = render_scale.estimate_render_transform(
        pairs,
        recorded_viewport=tuple(case["recorded_viewport"]),
        live_viewport=png_size(case["frame"]),
    )
    return transform


def score(cases: list[dict], grounder: Any) -> list[dict]:
    from openadapt_flow import vision
    from openadapt_flow.runtime.resolver import png_size, resolve
    from openadapt_flow.vision.ocr import OcrResolutionRefused

    rows = []
    transforms: dict[int, Any] = {}
    for case in cases:
        started = time.monotonic()
        frame, transform = case["frame"], None
        if case.get("recorded_viewport") and png_size(frame) != tuple(
            case["recorded_viewport"]
        ):
            key = id(frame)
            if key not in transforms:
                transforms[key] = _render_transform(case, vision)
            transform = transforms[key]
        try:
            if (
                case.get("recorded_viewport")
                and frame is not None
                and (png_size(frame) != tuple(case["recorded_viewport"]))
            ):
                if transform is None:
                    # As replayed, the layout-drift check stops the step.
                    raise _LayoutDrift()
                frame = transform.normalize_png(frame)
            res = resolve(
                case["anchor"],
                frame,
                vision,
                grounder,
                template_png=case["template"],
                viewport=png_size(frame),
                structural=None,
            )
        except (OcrResolutionRefused, _LayoutDrift) as exc:
            outcome, rung, point, reason = "refused", None, None, type(exc).__name__
        else:
            if res is None:
                outcome, rung, point, reason = "miss", None, None, None
            else:
                point = res[0].point
                if transform is not None:
                    point = transform.to_live(*point)
                rung = res[0].rung
                outcome = "correct" if _inside(point, case["truth_box"]) else "wrong"
                reason = None
        rows.append(
            {
                "suite": case["suite"],
                "condition": case["condition"],
                "target": case["target"],
                "outcome": outcome,
                "rung": rung,
                "point": list(point) if point else None,
                "refusal": reason,
                "truth_box": [round(v, 1) for v in case["truth_box"]],
                "ms": round((time.monotonic() - started) * 1000.0, 1),
            }
        )
    return rows


def summarize(rows: list[dict]) -> dict:
    out: dict[str, Any] = {}
    for suite in sorted({r["suite"] for r in rows}):
        sub = [r for r in rows if r["suite"] == suite]
        entry = {
            k: sum(1 for r in sub if r["outcome"] == k)
            for k in ("correct", "wrong", "refused", "miss")
        }
        entry["n"] = len(sub)
        entry["by_condition"] = {
            c: {
                k: sum(1 for r in sub if r["condition"] == c and r["outcome"] == k)
                for k in ("correct", "wrong", "refused", "miss")
            }
            for c in sorted({r["condition"] for r in sub})
        }
        entry["median_ms"] = round(statistics.median(r["ms"] for r in sub), 1)
        out[suite] = entry
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--local-grounding", action="store_true")
    parser.add_argument("--avc444", nargs="*", type=Path, default=[])
    parser.add_argument(
        "--rdp-live",
        nargs="*",
        type=Path,
        default=[],
        help="rdp_capture.py output folders; the first is the recording",
    )
    parser.add_argument(
        "--suites", default="drift21,controls,avc444,rdp_live", help="comma-separated"
    )
    args = parser.parse_args()
    suites = set(args.suites.split(","))

    grounder = None
    if args.local_grounding:
        from openadapt_flow.runtime.local_grounder import LocalGroundingModel

        grounder = LocalGroundingModel.load()

    rows: list[dict] = []
    with tempfile.TemporaryDirectory() as tmpdir:
        if "drift21" in suites:
            rows += score(drift21_cases(), grounder)
        if "controls" in suites:
            rows += score(controls_cases(), grounder)
        if "avc444" in suites and args.avc444:
            rows += score(avc444_cases(args.avc444, Path(tmpdir)), grounder)
        if "rdp_live" in suites and args.rdp_live:
            rows += score(rdp_live_cases(args.rdp_live), grounder)

    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        cwd=HERE,
    ).stdout.strip()
    report = {
        "schema": "openadapt.pixel-drift-arms.v1",
        "arm": args.arm,
        "local_grounding": args.local_grounding,
        "commit": commit,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": {"machine": platform.machine(), "system": platform.system()},
        "summary": summarize(rows),
        "rows": rows,
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"arm": args.arm, "summary": report["summary"]}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
