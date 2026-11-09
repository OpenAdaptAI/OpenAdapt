"""Generate the OpenAdapt "How it works" website media from REAL runs.

This orchestrator drives the *actual* openadapt-flow pipeline against the
bundled MockMed demo app (and, optionally, a live OpenEMR instance) with the
headed browser + the opt-in Playwright video capture added to the recorder and
replayer, then renders web-optimized clips for the five landing-page steps:

    record   — film the recorder driving the real add-note task ("● REC")
    compile  — a crafted annotation over a REAL recorded frame + REAL bundle
               fields (the one step with no live UI)
    run      — film the compiled replay driving the app ("replaying·local·$0")
    heal     — film a drift replay healing live, ending on the REAL anchor diff
    audit    — the REAL illustrated REPORT.md rendered as a scroll clip + poster

Per step it writes ``<step>.webm`` (VP9), ``<step>.mp4`` (H.264, faststart),
``<step>.gif`` (gifski) and ``<step>.jpg`` (poster) into ``--out``, and updates
``MANIFEST.json``.

Everything the site would caption as "real product footage" IS real product
footage; only ``compile`` is a crafted annotation (honestly built from a real
frame + the real ``workflow.py`` fields).  Presentation overlays (the badges,
the closing diff card, the captions) are added by THIS script in post; they are
not part of the product and never touch the app under test.

Nothing here runs in normal use — it is a developer/marketing tool.  The
library capability it relies on (``--record-video`` / ``record_video_dir=``) is
opt-in and off by default.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from PIL import Image, ImageDraw, ImageFont

# -- constants ---------------------------------------------------------------

OUT_W = 880  # capped display width for the landing page
FPS = 14
GIF_FPS = 12
NOTE_TEXT = "Follow-up in 2 weeks; BP recheck."

# macOS system fonts (this generator is run on the maintainer's Mac).
_FONT_SANS = "/System/Library/Fonts/SFNS.ttf"
_FONT_SANS_B = "/System/Library/Fonts/SFNSRounded.ttf"
_FONT_MONO = "/System/Library/Fonts/SFNSMono.ttf"
_FONT_HELV = "/System/Library/Fonts/Helvetica.ttc"

# Palette (matches the site's dark surface).
INK = (14, 18, 27)
PANEL = (22, 27, 38)
CARD = (30, 36, 50)
LINE = (54, 62, 82)
TEXT = (232, 236, 244)
MUTED = (150, 160, 178)
ACCENT = (96, 165, 250)  # blue
GOOD = (52, 211, 153)  # green
WARN = (251, 191, 36)  # amber
BAD = (248, 113, 113)  # red
DIFF_ADD = (34, 197, 94)
DIFF_DEL = (239, 68, 68)


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.truetype(_FONT_HELV, size)


# -- shell / ffmpeg helpers --------------------------------------------------


def _run(cmd: Sequence[str], quiet: bool = True) -> None:
    """Run a subprocess, raising on failure."""
    res = subprocess.run(
        list(cmd),
        stdout=subprocess.DEVNULL if quiet else None,
        stderr=subprocess.PIPE,
        text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"command failed ({res.returncode}): {' '.join(cmd)}\n{res.stderr}"
        )


def _frame_count(video: Path) -> int:
    """Exact decoded frame count (webm duration metadata is unreliable)."""
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_frames",
            "-show_entries",
            "stream=nb_read_frames",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video),
        ],
        capture_output=True,
        text=True,
    )
    try:
        return int(out.stdout.strip())
    except ValueError:
        return 0


def _src_fps(video: Path) -> float:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=r_frame_rate",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video),
        ],
        capture_output=True,
        text=True,
    )
    txt = out.stdout.strip()
    if "/" in txt:
        n, d = txt.split("/")
        return float(n) / float(d) if float(d) else 25.0
    return float(txt or 25.0)


def _duration(video: Path) -> float:
    fps = _src_fps(video)
    return _frame_count(video) / fps if fps else 0.0


# -- overlay badges ----------------------------------------------------------


def _rounded(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    radius: int,
    fill,
) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def _badge_png(
    out: Path,
    text: str,
    *,
    dot: Optional[tuple] = None,
    triangle: Optional[tuple] = None,
    dot_ring: bool = False,
) -> tuple[int, int]:
    """Render a translucent pill badge (RGBA) sized for 880-wide output."""
    font = _font(_FONT_SANS_B, 26)
    pad_x, pad_y, gap = 22, 14, 14
    glyph_w = 26 if (dot or triangle) else 0
    tmp = Image.new("RGBA", (10, 10))
    tw = ImageDraw.Draw(tmp).textbbox((0, 0), text, font=font)
    text_w = tw[2] - tw[0]
    text_h = tw[3] - tw[1]
    w = pad_x * 2 + glyph_w + (gap if glyph_w else 0) + text_w
    h = pad_y * 2 + max(text_h, 26)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    _rounded(d, (0, 0, w - 1, h - 1), h // 2, (10, 13, 20, 210))
    d.rounded_rectangle(
        (0, 0, w - 1, h - 1), radius=h // 2, outline=(255, 255, 255, 40), width=1
    )
    cx = pad_x
    cy = h // 2
    if dot is not None:
        r = 8
        if dot_ring:
            d.ellipse(
                (cx - r - 4, cy - r - 4, cx + r + 4, cy + r + 4),
                outline=dot + (110,),
                width=2,
            )
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=dot + (255,))
        cx += glyph_w + gap
    elif triangle is not None:
        d.polygon(
            [(cx, cy - 10), (cx, cy + 10), (cx + 16, cy)],
            fill=triangle + (255,),
        )
        cx += glyph_w + gap
    d.text((cx, cy - text_h / 2 - tw[1]), text, font=font, fill=(240, 244, 252, 255))
    img.save(out)
    return img.size


# -- master encode + finalize ------------------------------------------------


@dataclass
class Overlay:
    """A badge composited at a fixed corner of the master video."""

    png: Path
    x: str  # ffmpeg overlay x expr (may reference W/w)
    y: str
    blink: bool = False


def _build_master(
    raw: Path,
    master: Path,
    *,
    trim_start: float = 0.0,
    trim_end: Optional[float] = None,
    speed: float = 1.0,
    overlays: Sequence[Overlay] = (),
    fps: int = FPS,
    width: int = OUT_W,
) -> None:
    """Normalize a raw capture into the H.264 master (overlays burned in)."""
    vf = [
        f"trim=start={trim_start}"
        + (f":end={trim_end}" if trim_end is not None else ""),
        "setpts=PTS-STARTPTS",
    ]
    if speed != 1.0:
        vf.append(f"setpts=PTS/{speed}")
    vf.append(f"scale={width}:-2:flags=lanczos")
    vf.append(f"fps={fps}")
    chain = f"[0:v]{','.join(vf)}[base]"
    inputs = ["-i", str(raw)]
    parts = [chain]
    last = "base"
    for idx, ov in enumerate(overlays):
        inputs += ["-i", str(ov.png)]
        label = f"o{idx}"
        enable = (
            ":enable='lt(mod(t,1),0.55)'" if ov.blink else ""
        )
        parts.append(
            f"[{last}][{idx + 1}:v]overlay={ov.x}:{ov.y}{enable}[{label}]"
        )
        last = label
    filtergraph = ";".join(parts)
    _run(
        [
            "ffmpeg",
            "-y",
            *inputs,
            "-filter_complex",
            filtergraph,
            "-map",
            f"[{last}]",
            "-c:v",
            "libx264",
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "23",
            "-movflags",
            "+faststart",
            "-an",
            str(master),
        ]
    )


def _master_from_frames(
    frames_dir: Path, master: Path, *, fps: int = FPS, width: int = OUT_W
) -> None:
    """Encode a directory of zero-padded PNG frames into the H.264 master."""
    _run(
        [
            "ffmpeg",
            "-y",
            "-framerate",
            str(fps),
            "-i",
            str(frames_dir / "f%05d.png"),
            "-vf",
            f"scale={width}:-2:flags=lanczos,fps={fps}",
            "-c:v",
            "libx264",
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "22",
            "-movflags",
            "+faststart",
            "-an",
            str(master),
        ]
    )


def _finalize(
    master: Path,
    out_base: Path,
    *,
    poster_at: float = 0.0,
    gif_width: int = OUT_W,
    gif_fps: int = GIF_FPS,
    gif_max_s: Optional[float] = None,
) -> dict:
    """Emit webm + mp4 + gif + jpg from the master; return their byte sizes.

    ``gif_width`` / ``gif_fps`` / ``gif_max_s`` bound the GIF fallback so a
    long, detail-dense clip (the scrolling audit report) still fits the
    landing-page budget; the webm/mp4 always carry the full-quality clip.
    """
    webm = out_base.with_suffix(".webm")
    mp4 = out_base.with_suffix(".mp4")
    gif = out_base.with_suffix(".gif")
    jpg = out_base.with_suffix(".jpg")

    # mp4 IS the master (already H.264 + faststart).
    shutil.copyfile(master, mp4)

    # VP9 webm — small, high quality, muted.
    _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(master),
            "-c:v",
            "libvpx-vp9",
            "-b:v",
            "0",
            "-crf",
            "36",
            "-row-mt",
            "1",
            "-an",
            str(webm),
        ]
    )

    # Poster: a single JPEG still at poster_at seconds.
    _run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            str(poster_at),
            "-i",
            str(master),
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(jpg),
        ]
    )

    # GIF via gifski (palette-optimized). Extract frames at gif_fps first.
    with tempfile.TemporaryDirectory() as td:
        fdir = Path(td)
        cap = ["-t", str(gif_max_s)] if gif_max_s else []
        _run(
            [
                "ffmpeg",
                "-y",
                *cap,
                "-i",
                str(master),
                "-vf",
                f"fps={gif_fps},scale={gif_width}:-2:flags=lanczos",
                str(fdir / "g%05d.png"),
            ]
        )
        frames = sorted(fdir.glob("g*.png"))
        if shutil.which("gifski") and frames:
            _run(
                [
                    "gifski",
                    "--fps",
                    str(gif_fps),
                    "--width",
                    str(gif_width),
                    "--quality",
                    "80",
                    "-o",
                    str(gif),
                    *[str(f) for f in frames],
                ]
            )
        else:  # fallback: ffmpeg palettegen
            _run(
                [
                    "ffmpeg",
                    "-y",
                    *cap,
                    "-i",
                    str(master),
                    "-vf",
                    f"fps={gif_fps},scale={gif_width}:-2:flags=lanczos,"
                    "split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
                    str(gif),
                ]
            )

    dims = _mp4_dims(mp4)
    return {
        "webm": webm.stat().st_size,
        "mp4": mp4.stat().st_size,
        "gif": gif.stat().st_size,
        "jpg": jpg.stat().st_size,
        "width": dims[0],
        "height": dims[1],
    }


def _mp4_dims(mp4: Path) -> tuple[int, int]:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=s=x:p=0",
            str(mp4),
        ],
        capture_output=True,
        text=True,
    ).stdout.strip()
    w, h = out.split("x")
    return int(w), int(h)


def _newest_webm(directory: Path) -> Path:
    vids = sorted(directory.glob("*.webm"), key=lambda p: p.stat().st_mtime)
    if not vids:
        raise FileNotFoundError(f"no .webm captured in {directory}")
    return vids[-1]


# -- manifest ----------------------------------------------------------------

MANIFEST_ALT = {
    "record": "Screen recording of a nurse completing an add-note task in a "
    "clinical web app while OpenAdapt captures every screen and input.",
    "compile": "The recorded demonstration compiled into an editable script: "
    "visual anchor targets, a per-step assertion, and a parameter.",
    "run": "OpenAdapt replaying the compiled workflow automatically and "
    "locally in seconds, with no per-run model calls.",
    "heal": "OpenAdapt replaying against a drifted UI: the fallback ladder "
    "still finds each target and proposes the anchor fix as a reviewable diff.",
    "audit": "The illustrated run report OpenAdapt produces for every run: "
    "what ran, what it saw, and what changed, with per-step screenshots.",
    "record_openemr": "OpenAdapt's recorder capturing a real session in a "
    "live OpenEMR electronic health record: signing in and opening the patient "
    "workflow while every screen and input is captured.",
    "run_openemr": "OpenAdapt replaying a compiled workflow against a live "
    "OpenEMR electronic health record, locally and with no per-run model calls.",
}


_APP_LABEL = {
    "mockmed": "our MockMed demo app",
    "openemr": "a live OpenEMR instance",
}
_STEP_TITLE = {
    "record": "Record",
    "compile": "Compile",
    "run": "Run",
    "heal": "Self-heal",
    "audit": "Audit",
}


def _update_manifest(out_dir: Path, key: str, sizes: dict, source: str) -> None:
    path = out_dir / "MANIFEST.json"
    data = {}
    if path.exists():
        data = json.loads(path.read_text())
    steps = data.setdefault("steps", {})
    canonical = key.replace("_openemr", "")
    app = sizes.get("app", "mockmed")
    steps[key] = {
        "step": canonical,  # which of the 5 landing-page steps this serves
        "webm": f"{key}.webm",
        "mp4": f"{key}.mp4",
        "gif": f"{key}.gif",
        "poster": f"{key}.jpg",
        "width": sizes["width"],
        "height": sizes["height"],
        "source": source,  # "real" | "crafted"
        "app": app,
        # Honest, site-ready caption (real footage vs the crafted Compile).
        "caption": (
            f"{_STEP_TITLE[canonical]} — "
            + (
                "crafted annotation over a real recorded frame"
                if source == "crafted"
                else f"real footage · driving {_APP_LABEL.get(app, app)}"
            )
        ),
        "alt": MANIFEST_ALT[key],
        "bytes": {
            "webm": sizes["webm"],
            "mp4": sizes["mp4"],
            "gif": sizes["gif"],
            "jpg": sizes["jpg"],
        },
    }
    # Canonical 5-step order + which keys serve each step (default variant
    # first, then any real-EMR variant), so the embed can pick per step.
    data["order"] = ["record", "compile", "run", "heal", "audit"]
    variants: dict[str, list[str]] = {s: [] for s in data["order"]}
    for k, v in steps.items():
        variants.setdefault(v["step"], [])
        if k not in variants[v["step"]]:
            variants[v["step"]].append(k)
    for s in variants:
        variants[s].sort(key=lambda k: ("_openemr" in k, k))
    data["variants"] = variants
    path.write_text(json.dumps(data, indent=2) + "\n")


# -- live capture (real product footage) -------------------------------------

_VP = {"width": 1280, "height": 800}


def _serve_mockmed(drift: Optional[str] = None):
    from openadapt_flow.mockmed.server import serve

    url, stop = serve(port=0)
    if drift:
        url = url.rstrip("/") + f"/?drift={drift}"
    return url, stop


def capture_record(work: Path, *, headed: bool = True) -> tuple[Path, Path]:
    """Film the recorder driving the real MockMed add-note task.

    Returns ``(raw_webm, recording_dir)``.
    """
    from openadapt_flow.demo_driver import record_triage_demo

    vid_dir = work / "record_video"
    vid_dir.mkdir(parents=True, exist_ok=True)
    rec_dir = work / "recording"
    url, stop = _serve_mockmed()
    try:
        record_triage_demo(
            url,
            rec_dir,
            note_text=NOTE_TEXT,
            headed=headed,
            record_video_dir=vid_dir,
        )
    finally:
        stop()
    return _newest_webm(vid_dir), rec_dir


def _replay_with_video(
    bundle: Path,
    run_dir: Path,
    vid_dir: Path,
    *,
    drift: Optional[str] = None,
    save_healed_to: Optional[Path] = None,
    headed: bool = True,
) -> Path:
    """Run the real Replayer against MockMed while filming the session.

    Returns the raw ``.webm``. Mirrors ``__main__._cmd_replay`` wiring.
    """
    from playwright.sync_api import sync_playwright

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.ir import Workflow
    from openadapt_flow.report import render_run_report
    from openadapt_flow.runtime import Replayer
    from openadapt_flow.runtime.grounder import build_grounder

    vid_dir.mkdir(parents=True, exist_ok=True)
    workflow = Workflow.load(bundle)
    url, stop = _serve_mockmed(drift)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not headed)
            context = browser.new_context(
                viewport=_VP,
                device_scale_factor=1,
                record_video_dir=str(vid_dir),
                record_video_size=_VP,
            )
            page = context.new_page()
            page.goto(url)
            try:
                backend = PlaywrightBackend(page)
                grounder = build_grounder()
                Replayer(
                    backend,
                    grounder=grounder,
                    use_structural=not bool(drift),
                ).run(
                    workflow,
                    bundle_dir=bundle,
                    run_dir=run_dir,
                    save_healed_to=save_healed_to,
                )
            finally:
                context.close()
                browser.close()
    finally:
        stop()
    render_run_report(run_dir)
    return _newest_webm(vid_dir)


# -- step 1: RECORD ----------------------------------------------------------


def build_record(work: Path, out: Path, *, headed: bool) -> tuple[Path, Path]:
    raw, rec_dir = capture_record(work, headed=headed)
    badge = work / "badge_rec.png"
    bw, bh = _badge_png(badge, "REC", dot=BAD, dot_ring=True)
    master = work / "record_master.mp4"
    _build_master(
        raw,
        master,
        trim_start=0.25,
        speed=1.0,
        overlays=[Overlay(badge, x="28", y="26", blink=True)],
    )
    sizes = _finalize(master, out / "record", poster_at=0.5)
    sizes["app"] = "mockmed"
    _update_manifest(out, "record", sizes, "real")
    print(f"  record: {sizes}")
    return raw, rec_dir


# -- step 3: RUN -------------------------------------------------------------


def build_run(work: Path, out: Path, bundle: Path, *, headed: bool) -> Path:
    run_dir = work / "run_clean"
    raw = _replay_with_video(bundle, run_dir, work / "run_video", headed=headed)
    badge = work / "badge_run.png"
    _badge_png(badge, "replaying · local · $0", triangle=GOOD)
    master = work / "run_master.mp4"
    _build_master(
        raw,
        master,
        trim_start=0.8,
        speed=1.35,
        overlays=[Overlay(badge, x="28", y="26")],
    )
    sizes = _finalize(master, out / "run", poster_at=0.3)
    sizes["app"] = "mockmed"
    _update_manifest(out, "run", sizes, "real")
    print(f"  run: {sizes}")
    return run_dir


# -- step 4: SELF-HEAL -------------------------------------------------------


def _diff_card(patch: dict, heal: dict, base_frame: Path, card: Path) -> None:
    """Render the closing anchor-diff card from REAL patch.json/heal.json."""
    W, H = 1280, 800
    img = Image.new("RGB", (W, H), INK)
    d = ImageDraw.Draw(img)
    f_title = _font(_FONT_SANS_B, 34)
    f_sub = _font(_FONT_SANS, 22)
    f_mono = _font(_FONT_MONO, 26)
    f_small = _font(_FONT_SANS, 20)

    step_id = patch.get("step_id", "step")
    rung = patch.get("rung_used", "?")
    d.text((80, 70), "Target found under drift", font=f_title, fill=TEXT)
    d.text(
        (80, 118),
        f"{step_id} · resolved via the {rung} rung · fix proposed as a diff",
        font=f_sub,
        fill=MUTED,
    )

    # Diff panel
    px, py, pw, ph = 80, 190, W - 160, 250
    d.rounded_rectangle((px, py, px + pw, py + ph), radius=16, fill=CARD)
    d.rounded_rectangle(
        (px, py, px + pw, py + ph), radius=16, outline=LINE, width=1
    )
    y = py + 26
    changes = patch.get("changes", [])
    for ch in changes:
        field_name = ch.get("field")
        old = ch.get("old")
        new = ch.get("new")
        ident = " (identity — verified unchanged)" if ch.get("identity") else ""
        if field_name == "context_text" and ch.get("identity"):
            # identity text is normalized, not "changed" — render as confirmed.
            ident_txt = str(new)
            if len(ident_txt) > 34:
                ident_txt = ident_txt[:33] + "…"
            line = f"  identity  {ident_txt}"
            d.text((px + 30, y), line, font=f_mono, fill=GOOD)
            lw = d.textbbox((0, 0), line, font=f_mono)[2]
            d.text(
                (px + 30 + lw + 24, y + 4),
                "✓ same record",
                font=f_small,
                fill=GOOD,
            )
            y += 46
            continue
        d.text((px + 30, y), f"- {field_name}: {old!r}", font=f_mono, fill=DIFF_DEL)
        y += 42
        d.text((px + 30, y), f"+ {field_name}: {new!r}{ident}", font=f_mono, fill=DIFF_ADD)
        y += 52

    d.text(
        (px + 30, py + ph - 44),
        "reviewable · nothing applied without sign-off",
        font=f_small,
        fill=MUTED,
    )

    # Footer status
    status = patch.get("status", "")
    d.text(
        (80, py + ph + 40),
        f"anchor patch: {status}   ·   0 model calls   ·   $0",
        font=f_sub,
        fill=ACCENT,
    )
    img.save(card)


def _pick_heal(heals_dir: Path) -> str:
    """Choose the healed step with the most legible diff (a label rename)."""
    patches = sorted(heals_dir.glob("*/patch.json"))
    if not patches:
        raise FileNotFoundError(f"no heals under {heals_dir}")
    with_rename = []
    for pf in patches:
        data = json.loads(pf.read_text())
        if any(
            c.get("field") == "ocr_text" and not c.get("identity")
            for c in data.get("changes", [])
        ):
            with_rename.append(pf.parent.name)
    return (with_rename or [p.parent.name for p in patches])[0]


def build_heal(work: Path, out: Path, bundle: Path, *, headed: bool) -> Path:
    run_dir = work / "run_drift"
    healed = work / "healed_bundle"
    # `theme,rename` = an obviously drifted UI (dark theme) plus a relabel
    # (Open->View, Save->Submit) that forces the anchor ladder to heal and
    # yields the clearest reviewable diff (ocr_text: Open -> View at step_005).
    raw = _replay_with_video(
        bundle,
        run_dir,
        work / "heal_video",
        drift="theme,rename",
        save_healed_to=healed,
        headed=headed,
    )
    badge = work / "badge_heal.png"
    _badge_png(badge, "UI drift · self-healing", dot=WARN)
    # Live drift-replay segment.
    live = work / "heal_live.mp4"
    _build_master(
        raw,
        live,
        trim_start=0.8,
        speed=1.4,
        overlays=[Overlay(badge, x="28", y="26")],
    )
    # Closing diff card from a REAL patch: prefer the heal whose patch carries
    # a label rename (an ocr_text change) — the most legible drift-to-fix story.
    heals_dir = run_dir / "heals"
    pick = _pick_heal(heals_dir)
    patch = json.loads((heals_dir / pick / "patch.json").read_text())
    heal = json.loads((heals_dir / pick / "heal.json").read_text())
    card = work / "heal_card.png"
    _diff_card(patch, heal, run_dir / "steps" / f"{pick}_before.png", card)
    card_mp4 = work / "heal_card.mp4"
    _run(
        [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-t",
            "3.2",
            "-i",
            str(card),
            "-vf",
            f"scale={OUT_W}:-2:flags=lanczos,fps={FPS},"
            "fade=t=in:st=0:d=0.3",
            "-c:v",
            "libx264",
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "22",
            "-movflags",
            "+faststart",
            "-an",
            str(card_mp4),
        ]
    )
    # Concatenate live + card into the heal master.
    master = work / "heal_master.mp4"
    _concat([live, card_mp4], master)
    sizes = _finalize(master, out / "heal", poster_at=0.3)
    sizes["app"] = "mockmed"
    _update_manifest(out, "heal", sizes, "real")
    print(f"  heal: {sizes} (diff from {pick})")
    return run_dir


def _concat(parts: Sequence[Path], out: Path) -> None:
    """Concatenate MP4 parts (re-encode for uniform params)."""
    with tempfile.TemporaryDirectory() as td:
        listf = Path(td) / "list.txt"
        listf.write_text("".join(f"file '{p}'\n" for p in parts))
        _run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(listf),
                "-c:v",
                "libx264",
                "-profile:v",
                "high",
                "-pix_fmt",
                "yuv420p",
                "-crf",
                "23",
                "-movflags",
                "+faststart",
                "-an",
                str(out),
            ]
        )


# -- step 2: COMPILE (crafted annotation over a REAL frame + REAL fields) -----


def _ease(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def _ramp(t: float, start: float, dur: float) -> float:
    return _ease((t - start) / dur) if dur else (1.0 if t >= start else 0.0)


def _paste_alpha(base: Image.Image, layer: Image.Image, alpha: float) -> None:
    if alpha <= 0:
        return
    if alpha < 1:
        a = layer.split()[3].point(lambda v: int(v * alpha))
        layer = layer.copy()
        layer.putalpha(a)
    base.alpha_composite(layer)


def _label(
    draw_img: Image.Image,
    xy: tuple[int, int],
    tag: str,
    text: str,
    color: tuple,
) -> None:
    d = ImageDraw.Draw(draw_img)
    f_tag = _font(_FONT_SANS_B, 20)
    f_txt = _font(_FONT_MONO, 22)
    tb = d.textbbox((0, 0), tag, font=f_tag)
    xb = d.textbbox((0, 0), text, font=f_txt)
    tw = (tb[2] - tb[0]) + 20
    ww = max(tw + 12 + (xb[2] - xb[0]), 40)
    h = 44
    x, y = xy
    d.rounded_rectangle((x, y, x + ww + 24, y + h), radius=10, fill=(18, 22, 32, 235))
    d.rounded_rectangle(
        (x, y, x + ww + 24, y + h), radius=10, outline=color + (255,), width=2
    )
    d.rounded_rectangle((x + 10, y + 9, x + 10 + tw, y + h - 9), radius=6, fill=color + (255,))
    d.text((x + 20, y + 12), tag, font=f_tag, fill=(10, 13, 20, 255))
    d.text((x + 22 + tw, y + 10), text, font=f_txt, fill=(232, 236, 244, 255))


def _anchor_box(size, region, color, alpha_line) -> Image.Image:
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    x, y, w, h = region
    d.rectangle((x - 3, y - 3, x + w + 3, y + h + 3), outline=color + (255,), width=4)
    # corner ticks
    for cx, cy in ((x - 3, y - 3), (x + w + 3, y - 3), (x - 3, y + h + 3), (x + w + 3, y + h + 3)):
        d.rectangle((cx - 5, cy - 5, cx + 5, cy + 5), fill=color + (255,))
    return layer


def build_compile(work: Path, out: Path, bundle: Path, base_frame: Path) -> None:
    wf = json.loads((bundle / "workflow.json").read_text())
    steps = {s["id"]: s for s in wf["steps"]}
    save = steps["step_010"]
    note_click = steps["step_008"]  # the Note-field focusing click
    param_val = wf["params"].get("note", NOTE_TEXT)
    save_region = save["anchor"]["region"]
    note_region = note_click["anchor"]["region"]
    save_sel = save["anchor"]["structural"]["selector"]
    assertion = next(
        (e for e in save["expect"] if e["kind"] == "text_present"), None
    )
    assert_text = assertion["text"] if assertion else "Encounter saved-"
    risk = save["risk"]

    base = Image.open(base_frame).convert("RGBA").resize((1280, 800))

    # Pre-render static layers.
    vignette = Image.new("RGBA", (1280, 800), (0, 0, 0, 0))
    ImageDraw.Draw(vignette).rectangle((0, 0, 1280, 800), fill=(8, 11, 18, 90))

    box_save = _anchor_box((1280, 800), save_region, ACCENT, 255)

    lbl_anchor = Image.new("RGBA", (1280, 800), (0, 0, 0, 0))
    _label(lbl_anchor, (save_region[0] + save_region[2] + 24, save_region[1] - 6),
           "anchor", save_sel, ACCENT)

    lbl_assert = Image.new("RGBA", (1280, 800), (0, 0, 0, 0))
    _label(lbl_assert, (save_region[0] + save_region[2] + 24, save_region[1] + 54),
           "assert", f'text_present "{assert_text}"', WARN)
    # risk pill under the assert
    dr = ImageDraw.Draw(lbl_assert)
    rx, ry = save_region[0] + save_region[2] + 24, save_region[1] + 110
    dr.rounded_rectangle((rx, ry, rx + 210, ry + 40), radius=20, fill=BAD + (255,))
    dr.text((rx + 18, ry + 8), f"risk · {risk}", font=_font(_FONT_SANS_B, 22), fill=(10, 13, 20, 255))

    lbl_param = Image.new("RGBA", (1280, 800), (0, 0, 0, 0))
    _label(lbl_param, (note_region[0], note_region[1] - 58),
           "param", f'note = "{param_val}"', GOOD)

    # Code panel (real workflow.py step_010 excerpt), wrapped to fit.
    cp = tuple(save["anchor"]["click_point"])
    code_lines = [
        "# step_010  [irreversible]",
        "flow.click(",
        "  template='step_010.png',",
        f"  click_point={cp},",
        "  ocr_text='Save Encounter')",
        "",
        "# assert:",
        f"#   text_present '{assert_text}'",
        "",
        "PARAMS = {'note': <note>}",
    ]
    panel = _code_panel(code_lines, width=548, height=560)

    title_f = _font(_FONT_SANS_B, 40)
    sub_f = _font(_FONT_SANS, 24)

    frames_dir = work / "compile_frames"
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
    frames_dir.mkdir(parents=True)

    total = 8.0
    n = int(total * FPS)
    for i in range(n):
        t = i / FPS
        img = base.copy()
        _paste_alpha(img, vignette, min(1.0, _ramp(t, 0.0, 0.6) * 1.0))

        # header band
        head = Image.new("RGBA", (1280, 800), (0, 0, 0, 0))
        hd = ImageDraw.Draw(head)
        hd.rectangle((0, 0, 1280, 96), fill=(10, 13, 20, 210))
        hd.text((40, 24), "The demonstration becomes an editable script",
                font=title_f, fill=(232, 236, 244, 255))
        _paste_alpha(img, head, _ramp(t, 0.1, 0.5))

        pulse = 0.55 + 0.45 * abs(((t * 1.6) % 1.0) - 0.5) * 2
        _paste_alpha(img, box_save, _ramp(t, 0.6, 0.5) * pulse)
        _paste_alpha(img, lbl_anchor, _ramp(t, 0.9, 0.5))
        _paste_alpha(img, lbl_param, _ramp(t, 2.0, 0.5))
        _paste_alpha(img, lbl_assert, _ramp(t, 3.0, 0.6))

        # code panel slides in from the right in the last phase
        slide = _ramp(t, 4.6, 0.9)
        if slide > 0:
            shade = Image.new("RGBA", (1280, 800), (0, 0, 0, 0))
            ImageDraw.Draw(shade).rectangle((0, 96, 1280, 800), fill=(8, 11, 18, int(150 * slide)))
            _paste_alpha(img, shade, 1.0)
            px = int(1280 - (560 + 60) * slide)
            img.alpha_composite(panel, (px, 150))

        img.convert("RGB").save(frames_dir / f"f{i:05d}.png")

    master = work / "compile_master.mp4"
    _master_from_frames(frames_dir, master)
    sizes = _finalize(master, out / "compile", poster_at=3.2)
    sizes["app"] = "mockmed"
    _update_manifest(out, "compile", sizes, "crafted")
    print(f"  compile: {sizes}")


def _code_panel(lines: Sequence[str], width: int, height: int) -> Image.Image:
    panel = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(panel)
    d.rounded_rectangle((0, 0, width - 1, height - 1), radius=18, fill=(18, 22, 32, 255))
    d.rounded_rectangle((0, 0, width - 1, height - 1), radius=18, outline=LINE + (255,), width=1)
    # title bar dots
    for k, col in enumerate(((248, 113, 113), (251, 191, 36), (52, 211, 153))):
        d.ellipse((22 + k * 26, 20, 36 + k * 26, 34), fill=col + (255,))
    d.text((120, 18), "workflow.py", font=_font(_FONT_SANS_B, 22), fill=MUTED + (255,))
    f = _font(_FONT_MONO, 21)
    y = 70
    for ln in lines:
        color = MUTED if ln.strip().startswith("#") else TEXT
        if ln.strip().startswith("# expect"):
            color = WARN
        if "irreversible" in ln:
            color = (252, 165, 165)
        d.text((28, y), ln, font=f, fill=color + (255,))
        y += 34
    return panel


# -- step 5: AUDIT (the REAL illustrated report) -----------------------------

_REPORT_CSS = """
<style>
  html,body{margin:0;background:#0e121b;color:#e8ecf4;
    font:16px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;}
  .wrap{max-width:820px;margin:0 auto;padding:34px 30px 80px;}
  h1{font-size:30px;margin:.2em 0 .5em;} h2{font-size:22px;margin:1.4em 0 .5em;
    color:#cdd6e6;border-bottom:1px solid #263049;padding-bottom:.25em;}
  h3{font-size:18px;color:#9fb0d0;margin:1.1em 0 .4em;}
  table{border-collapse:collapse;width:100%;margin:.6em 0;font-size:14px;}
  th,td{border:1px solid #263049;padding:6px 9px;text-align:left;}
  th{background:#161b26;color:#9fb0d0;}
  code{background:#161b26;padding:1px 6px;border-radius:5px;color:#9fd0ff;
    font:13px 'SF Mono',Menlo,monospace;}
  img{max-width:100%;border:1px solid #263049;border-radius:8px;margin:6px 0;}
  ul{padding-left:1.2em;} a{color:#7cc0ff;}
</style>
"""


def build_audit(work: Path, out: Path, run_dir: Path, *, headed: bool) -> None:
    import markdown as md
    from playwright.sync_api import sync_playwright

    md_text = (run_dir / "REPORT.md").read_text()
    body = md.markdown(md_text, extensions=["tables", "fenced_code", "sane_lists"])
    html = f"<!doctype html><meta charset='utf-8'>{_REPORT_CSS}<div class='wrap'>{body}</div>"
    (run_dir / "report.html").write_text(html)
    file_url = (run_dir / "report.html").resolve().as_uri()

    vid_dir = work / "audit_video"
    vid_dir.mkdir(parents=True, exist_ok=True)
    view = {"width": 900, "height": 760}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(
            viewport=view, device_scale_factor=1,
            record_video_dir=str(vid_dir), record_video_size=view,
        )
        page = context.new_page()
        page.goto(file_url)
        page.wait_for_timeout(400)
        total = page.evaluate("document.body.scrollHeight")
        # Poster: top of the report.
        poster_png = work / "audit_poster.png"
        page.screenshot(path=str(poster_png))
        # Smooth auto-scroll to the bottom over ~6.5s.
        steps = 130
        for k in range(steps + 1):
            y = int((total - view["height"]) * (k / steps))
            page.evaluate(f"window.scrollTo(0,{y})")
            page.wait_for_timeout(48)
        page.wait_for_timeout(400)
        context.close()
        browser.close()
    raw = _newest_webm(vid_dir)
    badge = work / "badge_audit.png"
    _badge_png(badge, "illustrated run report", dot=ACCENT)
    master = work / "audit_master.mp4"
    _build_master(
        raw, master, trim_start=0.5, speed=1.0,
        overlays=[Overlay(badge, x="W-w-28", y="26")],
    )
    # The audit clip is long and text-dense; bound the GIF fallback hard
    # (short teaser, reduced width/fps) so it fits the landing-page budget.
    sizes = _finalize(
        master,
        out / "audit",
        poster_at=0.2,
        gif_width=520,
        gif_fps=8,
        gif_max_s=3.0,
    )
    # Prefer the crisp full-res top-of-report screenshot as the poster.
    _run(["ffmpeg", "-y", "-i", str(poster_png), "-vf",
          f"scale={OUT_W}:-2:flags=lanczos", "-q:v", "3",
          str((out / "audit.jpg"))])
    sizes["jpg"] = (out / "audit.jpg").stat().st_size
    sizes["app"] = "mockmed"
    _update_manifest(out, "audit", sizes, "real")
    print(f"  audit: {sizes}")


# -- orchestration -----------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, help="Web asset output directory")
    ap.add_argument("--work", default=None, help="Scratch dir (default: temp)")
    ap.add_argument(
        "--steps",
        default="record,compile,run,heal,audit",
        help="Comma-separated subset to (re)build",
    )
    ap.add_argument(
        "--openemr",
        action="store_true",
        help=(
            "Also capture the real-EMR variants (record_openemr, run_openemr) "
            "against a live OpenEMR (benchmark/openemr_live must be up); "
            "best-effort, falls back to the MockMed clips on any failure"
        ),
    )
    ap.add_argument("--headless", action="store_true", help="Capture headless")
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="hiw_"))
    work.mkdir(parents=True, exist_ok=True)
    headed = not args.headless
    want = set(s.strip() for s in args.steps.split(",") if s.strip())
    print(f"work dir: {work}")

    from openadapt_flow.compiler import compile_recording

    # Record (also yields the recording the whole pipeline consumes).
    rec_dir = work / "recording"
    if "record" in want or not rec_dir.exists():
        _, rec_dir = build_record(work, out, headed=headed)

    # Compile the recording into the bundle every downstream step needs.
    bundle = work / "bundle"
    if not bundle.exists():
        compile_recording(rec_dir, bundle, name="mockmed-triage")
        print(f"  compiled bundle -> {bundle}")

    run_dir = None
    if "run" in want:
        run_dir = build_run(work, out, bundle, headed=headed)
    if "compile" in want:
        # Need a clean base frame; run once if not already done.
        if run_dir is None:
            run_dir = work / "run_clean"
            if not (run_dir / "steps" / "step_010_before.png").exists():
                _replay_with_video(bundle, run_dir, work / "run_video", headed=headed)
        build_compile(work, out, bundle, run_dir / "steps" / "step_010_before.png")
    if "heal" in want:
        build_heal(work, out, bundle, headed=headed)
    if "audit" in want:
        if run_dir is None or not (run_dir / "REPORT.md").exists():
            run_dir = work / "run_clean"
            if not (run_dir / "REPORT.md").exists():
                _replay_with_video(bundle, run_dir, work / "run_video", headed=headed)
        build_audit(work, out, run_dir, headed=headed)

    # Optional real-EMR variants (best-effort; only on --openemr or explicit
    # step keys). Each falls back silently to the MockMed clip on any failure.
    if args.openemr or "record_openemr" in want:
        build_record_openemr(work, out, headed=headed)
    if args.openemr or "run_openemr" in want:
        build_run_openemr(work, out, headed=headed)

    print(f"\nAssets written to {out}")
    print(f"Manifest: {out / 'MANIFEST.json'}")
    return 0


# -- OpenEMR (real EMR) capture ----------------------------------------------
#
# Optional: film the product driving a REAL, local OpenEMR instance (from
# benchmark/openemr_live). OpenEMR's dense, iframe-heavy UI is hard to
# GUI-drive reliably, so this covers a short, stable real-EMR task (sign in ->
# dismiss the registration notice -> open the Patient workflow) through the
# ACTUAL openadapt-flow Recorder -- genuine product footage against a real EMR.
# Everything else falls back to MockMed. No real PHI: the stock docker install
# ships throwaway admin/pass creds and zero patient data.

OPENEMR_LOGIN = (
    "https://localhost:9390/interface/login/login.php?site=default"
)


def capture_record_openemr(work: Path, *, headed: bool) -> Optional[Path]:
    """Film the openadapt-flow Recorder driving a real OpenEMR session.

    Returns the raw ``.webm`` (or None if OpenEMR could not be driven cleanly).
    """
    from playwright.sync_api import sync_playwright

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.recorder import Recorder

    vid_dir = work / "openemr_record_video"
    vid_dir.mkdir(parents=True, exist_ok=True)
    rec_dir = work / "openemr_recording"

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not headed, args=["--ignore-certificate-errors"]
        )
        ctx = browser.new_context(
            viewport=_VP,
            device_scale_factor=1,
            ignore_https_errors=True,
            record_video_dir=str(vid_dir),
            record_video_size=_VP,
        )
        page = ctx.new_page()
        try:
            page.goto(OPENEMR_LOGIN, wait_until="domcontentloaded")
            backend = PlaywrightBackend(page)
            rec = Recorder(
                backend, rec_dir, app_url=OPENEMR_LOGIN, settle_timeout_s=6.0
            )

            def center(getter) -> tuple[int, int]:
                loc = getter()
                loc.wait_for(state="visible", timeout=15000)
                box = loc.bounding_box()
                if box is None:
                    raise RuntimeError("no bounding box")
                return (
                    int(box["x"] + box["width"] / 2),
                    int(box["y"] + box["height"] / 2),
                )

            # Sign in (throwaway stock creds).
            rec.click(*center(lambda: page.locator("input[name=authUser]")))
            rec.type_text("admin")
            rec.click(*center(lambda: page.locator("input[name=clearPass]")))
            rec.type_text("pass")
            rec.click(*center(lambda: page.locator("#login-button")))
            page.wait_for_timeout(4500)

            # Dismiss the product-registration notice, if shown.
            ask = page.get_by_role("button", name="Ask again later")
            if ask.count():
                rec.click(*center(lambda: ask.first))
                page.wait_for_timeout(1200)

            # Open the Patient workflow via the real top nav.
            rec.click(
                *center(lambda: page.get_by_text("Patient", exact=True).first)
            )
            page.wait_for_timeout(700)
            ns = page.get_by_text("New/Search", exact=False)
            if ns.count():
                rec.click(*center(lambda: ns.first))
                page.wait_for_timeout(2500)

            rec.finish()
            video = page.video
            raw = Path(video.path()) if video else None
        finally:
            ctx.close()
            browser.close()
    return raw if raw and raw.exists() else _newest_webm(vid_dir)


def build_record_openemr(work: Path, out: Path, *, headed: bool) -> bool:
    """Build the OpenEMR (real-EMR) variant of the Record clip. Best-effort."""
    try:
        raw = capture_record_openemr(work, headed=headed)
    except Exception as e:  # OpenEMR is fiddly; fall back silently to MockMed.
        print(f"  openemr record: SKIPPED ({type(e).__name__}: {e})")
        return False
    if raw is None:
        print("  openemr record: SKIPPED (no video)")
        return False
    badge = work / "badge_rec.png"
    if not badge.exists():
        _badge_png(badge, "REC", dot=BAD, dot_ring=True)
    master = work / "record_openemr_master.mp4"
    _build_master(
        raw,
        master,
        trim_start=0.4,
        speed=1.25,
        overlays=[Overlay(badge, x="28", y="26")],
    )
    sizes = _finalize(master, out / "record_openemr", poster_at=1.2)
    sizes["app"] = "openemr"
    _update_manifest(out, "record_openemr", sizes, "real")
    print(f"  record_openemr: {sizes}")
    return True


def _replay_openemr_with_video(
    bundle: Path, run_dir: Path, vid_dir: Path, *, headed: bool
) -> tuple[Path, bool]:
    """Replay a bundle against the live OpenEMR, filming it. (raw, success)."""
    from playwright.sync_api import sync_playwright

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.ir import Workflow
    from openadapt_flow.report import render_run_report
    from openadapt_flow.runtime import Replayer
    from openadapt_flow.runtime.grounder import build_grounder

    vid_dir.mkdir(parents=True, exist_ok=True)
    workflow = Workflow.load(bundle)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not headed, args=["--ignore-certificate-errors"]
        )
        ctx = browser.new_context(
            viewport=_VP,
            device_scale_factor=1,
            ignore_https_errors=True,
            record_video_dir=str(vid_dir),
            record_video_size=_VP,
        )
        page = ctx.new_page()
        page.goto(OPENEMR_LOGIN, wait_until="domcontentloaded")
        success = False
        try:
            backend = PlaywrightBackend(page)
            report = Replayer(backend, grounder=build_grounder()).run(
                workflow, bundle_dir=bundle, run_dir=run_dir
            )
            success = report.success
        finally:
            ctx.close()
            browser.close()
    render_run_report(run_dir)
    return _newest_webm(vid_dir), success


def build_run_openemr(work: Path, out: Path, *, headed: bool) -> bool:
    """Build the OpenEMR (real-EMR) Run clip by replaying the OpenEMR
    recording against the live instance. Best-effort; returns success."""
    from openadapt_flow.compiler import compile_recording

    rec_dir = work / "openemr_recording"
    if not (rec_dir / "events.jsonl").exists():
        raw = capture_record_openemr(work, headed=headed)
        if raw is None:
            print("  run_openemr: SKIPPED (no recording)")
            return False
    bundle = work / "openemr_bundle"
    if not bundle.exists():
        compile_recording(rec_dir, bundle, name="openemr-patient")
    run_dir = work / "run_openemr"
    try:
        raw, success = _replay_openemr_with_video(
            bundle, run_dir, work / "run_openemr_video", headed=headed
        )
    except Exception as e:
        print(f"  run_openemr: SKIPPED ({type(e).__name__}: {e})")
        return False
    steps_ok = _steps_ok(run_dir)
    # Require a meaningful clean drive: at least the sign-in + a couple of
    # navigation steps resolved without a mis-click halt. Otherwise MockMed's
    # Run clip stands (it is the reliable money shot).
    if steps_ok < 4:
        print(f"  run_openemr: SKIPPED (only {steps_ok} steps drove cleanly)")
        return False
    # No burned-in status badge: the openadapt-web hero paints the
    # "running · local · $0" indicator as a floating DOM system overlay on top
    # of this footage, so the footage itself must stay clean (a baked badge
    # would double up the indicator). See openadapt-web ReplayHero + PR #246.
    master = work / "run_openemr_master.mp4"
    _build_master(
        raw, master, trim_start=0.6, speed=1.4,
        overlays=[],
    )
    sizes = _finalize(master, out / "run_openemr", poster_at=1.0)
    sizes["app"] = "openemr"
    _update_manifest(out, "run_openemr", sizes, "real")
    print(f"  run_openemr: {sizes} (success={success}, {steps_ok} clean steps)")
    return True


def _steps_ok(run_dir: Path) -> int:
    rj = run_dir / "report.json"
    if not rj.exists():
        return 0
    data = json.loads(rj.read_text())
    return sum(1 for r in data.get("results", []) if r.get("ok"))


if __name__ == "__main__":
    sys.exit(main())
