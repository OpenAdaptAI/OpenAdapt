"""Render the three-panel launch video from the measured run data.

    python render.py --run-id launch-4 --cut full     # 60-90 s
    python render.py --run-id launch-4 --cut social   # 20 s

Inputs, all produced by ``pipeline.py`` and committed under ``results/``:

* ``agent_ledger.json``: every model call's start/end time and list-price cost.
* ``agent_run.json``: the agent run's outcome and the claim it saved.
* ``program.json``: the compiled program's steps, parameters and record check.
* ``batch.csv``: one row per attempt: time, outcome, read-back verdicts.
* screen recordings (``video/``) of the agent run and of chosen claims.

Every counter on screen is read from these files at the moment it applies.
Nothing is estimated. Time-lapsed stretches are labelled with their speed.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import statistics
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
W, H = 1920, 1080
FPS = 24

# Palette (dark). Brand green for "done", amber for "stopped", grey for idle.
BG = (11, 18, 32)
PANEL = (19, 29, 48)
INK = (234, 240, 248)
MUTED = (140, 155, 178)
GREEN = (52, 211, 153)
AMBER = (251, 191, 36)
RED = (248, 113, 113)
BLUE = (96, 165, 250)

COL_X = (40, 660, 1280)
COL_W = 600
VID_Y = 196
VID_H = 375

FONT_DIR = Path("/System/Library/Fonts")


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "HelveticaNeue.ttc"
    try:
        return ImageFont.truetype(str(FONT_DIR / name), size, index=1 if bold else 0)
    except OSError:
        return ImageFont.load_default()


MONO = None


def mono(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(FONT_DIR / "Menlo.ttc"), size)
    except OSError:
        return font(size)


# -- data ------------------------------------------------------------------------


@dataclass
class Attempt:
    record: int
    attempt: int
    outcome: str
    success: bool
    program_s: float
    readback_correct: bool
    verdicts: str
    halted_step: str
    halt_reason: str
    ui_changed: bool
    video: str
    model_calls: int
    load: Optional[float] = None


@dataclass
class Data:
    run: Path
    ledger: dict
    agent: dict
    program: dict
    attempts: list[Attempt]
    agent_video: Path
    change: Optional[dict] = None
    repair: Optional[dict] = None
    n_planned: int = 0
    paused: Optional[dict] = None
    extra: dict = field(default_factory=dict)

    @property
    def records(self) -> dict[int, list[Attempt]]:
        out: dict[int, list[Attempt]] = {}
        for a in self.attempts:
            out.setdefault(a.record, []).append(a)
        return out


def load(run_id: str) -> Data:
    run = HERE / "results" / run_id
    attempts = []
    for name in ("batch.csv",):
        with (run / name).open() as f:
            for r in csv.DictReader(f):
                attempts.append(
                    Attempt(
                        record=int(r["record"]),
                        attempt=int(r.get("attempt") or 1),
                        outcome=r["outcome"],
                        success=r["success"] == "True",
                        program_s=float(r["program_s"]),
                        readback_correct=r["readback_correct"] == "True",
                        verdicts=r["verifier_verdicts"],
                        halted_step=r["halted_step"],
                        halt_reason=r["halt_reason"],
                        ui_changed=r["ui_changed"] == "True",
                        video=r["video"],
                        model_calls=int(r["model_calls"]),
                        load=(
                            float(r["host_load_1m"]) if r.get("host_load_1m") else None
                        ),
                    )
                )
    videos = sorted((run / "video" / "agent").glob("*.webm"))
    meta = run / "batch_meta.json"
    return Data(
        run=run,
        ledger=json.loads((run / "agent_ledger.json").read_text()),
        agent=json.loads((run / "agent_run.json").read_text()),
        program=json.loads((run / "program.json").read_text()),
        attempts=attempts,
        agent_video=videos[-1],
        change=json.loads(meta.read_text()).get("ui_change")
        if meta.is_file()
        else None,
        n_planned=json.loads(meta.read_text()).get("n", 0) if meta.is_file() else 0,
        paused=(
            json.loads((run / "paused.json").read_text())
            if (run / "paused.json").is_file()
            else None
        ),
        repair=(
            json.loads((run / "repair.json").read_text())
            if (run / "repair.json").is_file()
            else None
        ),
    )


# -- source video frames -----------------------------------------------------------


class Clip:
    """Frames of one source video, extracted once at FPS and panel size."""

    def __init__(self, path: Path, cache: Path, size=(COL_W, VID_H)) -> None:
        self.dir = cache / path.stem
        if not self.dir.is_dir():
            self.dir.mkdir(parents=True)
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-i",
                    str(path),
                    "-vf",
                    f"fps={FPS},scale={size[0]}:{size[1]}",
                    "-q:v",
                    "3",
                    str(self.dir / "%05d.jpg"),
                ],
                check=True,
            )
        self.frames = sorted(self.dir.glob("*.jpg"))
        self.duration = len(self.frames) / FPS

    def at(self, t: float) -> Image.Image:
        i = max(0, min(len(self.frames) - 1, int(round(t * FPS))))
        return Image.open(self.frames[i]).convert("RGB")


def agent_video_offset(data: Data, clip: Clip) -> float:
    """Seconds into the agent's screen recording at which its first model
    call starts, found by matching the agent's own timestamped screenshots
    against the recording (median over all matches)."""

    import numpy as np

    started = data.ledger["started_at"]
    frames = sorted((data.run / "agent_frames").glob("*.png"))
    small = (80, 50)
    vids = [
        np.asarray(Image.open(p).convert("L").resize(small), dtype=np.float32)
        for p in clip.frames
    ]
    offsets = []
    for p in frames[1:]:
        ts = float(p.stem.split("_", 1)[1])
        img = np.asarray(Image.open(p).convert("L").resize(small), dtype=np.float32)
        errs = [float(((v - img) ** 2).mean()) for v in vids]
        best = int(np.argmin(errs))
        offsets.append(best / FPS - (ts - started))
    return statistics.median(offsets)


# -- drawing helpers ---------------------------------------------------------------


def text(d: ImageDraw.ImageDraw, xy, s, size=28, fill=INK, bold=False, anchor="la"):
    d.text(xy, s, font=font(size, bold), fill=fill, anchor=anchor)


def panel_header(d: ImageDraw.ImageDraw, col: int, n: str, title: str, sub: str):
    x = COL_X[col]
    d.rounded_rectangle((x, 40, x + COL_W, 1000), 18, fill=PANEL)
    text(d, (x + 24, 64), n, 22, MUTED, True)
    text(d, (x + 24, 96), title, 34, INK, True)
    text(d, (x + 24, 146), sub, 22, MUTED)


def video_box(canvas: Image.Image, col: int, frame: Optional[Image.Image], dim=False):
    x = COL_X[col]
    if frame is None:
        d = ImageDraw.Draw(canvas)
        d.rectangle((x, VID_Y, x + COL_W, VID_Y + VID_H), fill=(13, 20, 36))
        return
    if dim:
        frame = Image.blend(frame, Image.new("RGB", frame.size, BG), 0.55)
    canvas.paste(frame, (x, VID_Y))


def counter(d, col, row, label, value, color=INK):
    x = COL_X[col] + 24
    y = VID_Y + VID_H + 30 + row * 74
    text(d, (x, y), label, 22, MUTED)
    text(d, (x, y + 26), value, 38, color, True)


def badge(d, col, y, s, color):
    x = COL_X[col] + 24
    w = d.textlength(s, font=font(22, True)) + 28
    d.rounded_rectangle((x, y, x + w, y + 38), 19, outline=color, width=2)
    text(d, (x + 14, y + 8), s, 22, color, True)


def mmss(seconds: float) -> str:
    seconds = max(0.0, seconds)
    return f"{int(seconds // 60)}:{seconds % 60:04.1f}"


OBSERVE_ONLY = {"screenshot", "wait", "zoom", "cursor_position"}


def describe_action(a: dict) -> str:
    inp = a.get("input") or {}
    name = a["name"]
    if "coordinate" in inp and name != "scroll":
        x, y = inp["coordinate"]
        return f"{name} ({x}, {y})"
    if name == "type":
        return f"type '{inp.get('text', '')}'"
    if name == "key":
        return f"key {inp.get('text', '')}"
    if name == "scroll":
        return (
            f"scroll {inp.get('scroll_direction', '')} {inp.get('scroll_amount', '')}"
        )
    return name


def change_caption(data: "Data", att: "Attempt", steps: list[dict]) -> str:
    """What really happened on the first claim after the UI change."""

    if att.success and att.readback_correct:
        return "The app changed. The program still saved the claim, and it read back."
    save = steps[-1]["id"] if steps else ""
    if save and f"anchor of step '{save}'" in att.halt_reason:
        return (
            "The app changed. The program could not find the Save button it "
            "learned, so it stopped before saving."
        )
    if att.halted_step == save:
        return "The app changed. The program stopped before clicking a Save it could not verify."
    return (
        f"The app changed. The program stopped at {att.halted_step}, before saving; "
        "nothing was written."
    )


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


# -- state at a moment ---------------------------------------------------------------


def agent_state(data: Data, t: float) -> dict:
    """The agent's measured counters ``t`` seconds after its first call."""

    calls = data.ledger["calls"]
    started = data.ledger["started_at"]
    done = [c for c in calls if c["ended_s"] - started <= t]
    total = data.ledger["finished_at"] - started
    return {
        "elapsed": min(t, total),
        "calls": len(done),
        "cost": sum(c["cost_usd"] for c in done),
        "actions": sum(
            1 for c in done for a in c["actions"] if a["name"] not in OBSERVE_ONLY
        ),
        "log": [a for c in done for a in c["actions"] if a["name"] not in OBSERVE_ONLY],
        "finished": t >= total,
        "total": total,
    }


def load_note(data: Data) -> str:
    """How loaded the host was during the timed claims, if over its cores."""

    loads = [a.load for a in data.attempts if a.load is not None and a.success]
    if not loads or max(loads) <= 12:
        return ""
    return (
        f"timed on a shared host under load {min(loads):.0f}-{max(loads):.0f} "
        "(12 cores)"
    )


def batch_state(data: Data, upto: int) -> dict:
    """Counters over records 1..``upto`` (final attempt of each)."""

    recs = data.records
    finals = [recs[r][-1] for r in sorted(recs) if r <= upto]
    saved = [a for a in finals if a.success and a.readback_correct]
    stopped = [a for a in finals if not a.success]
    times = [a.program_s for a in saved]
    return {
        "done": len(finals),
        "saved": len(saved),
        "stopped": len(stopped),
        "median": statistics.median(times) if times else None,
        "model_calls": sum(a.model_calls for a in finals),
        "checks": sum(
            int(a.verdicts.split("/")[0]) for a in saved if "/" in a.verdicts
        ),
    }


# -- scenes -------------------------------------------------------------------------


@dataclass
class Scene:
    seconds: float
    draw: Callable[[Image.Image, float], None]
    caption: str = ""


def build(data: Data, cut: str, cache: Path) -> list[Scene]:
    agent_clip = Clip(data.agent_video, cache)
    offset = agent_video_offset(data, agent_clip)
    a_total = data.ledger["finished_at"] - data.ledger["started_at"]
    recs = data.records
    n_records = max(data.n_planned, max(recs) if recs else 0)
    steps = data.program["steps"]
    params = data.program["params"]
    checks = data.program["record_check"]
    first_ok = next(
        (r for r in sorted(recs) if recs[r][-1].success and recs[r][-1].video), None
    )
    program_clip = Clip(HERE / recs[first_ok][-1].video, cache) if first_ok else None
    change_rec = next((r for r in sorted(recs) if recs[r][0].ui_changed), None)
    change_clip = None
    if change_rec and recs[change_rec][0].video:
        change_clip = Clip(HERE / recs[change_rec][0].video, cache)

    def program_offset(clip: Clip, attempt: Attempt) -> float:
        # The recording starts at browser launch; the program is its last
        # program_s seconds (sign-in comes first and is not measured).
        return max(0.0, clip.duration - attempt.program_s - 0.3)

    agent_speed = 1.0 if cut == "full" else 6.0
    t_agent = a_total / agent_speed

    def left(canvas, d, t_agent_s, dim=False):
        st = agent_state(data, t_agent_s)
        panel_header(
            d,
            0,
            "1 · COMPUTER-USE AGENT",
            "Claude does it once",
            f"{data.ledger['model']} · computer use · live",
        )
        video_box(canvas, 0, agent_clip.at(offset + st["elapsed"]), dim=dim)
        counter(d, 0, 0, "Time", mmss(st["elapsed"]))
        counter(
            d,
            0,
            1,
            "Model calls · cost (list price)",
            f"{st['calls']} · ${st['cost']:.3f}",
        )
        counter(d, 0, 2, "Save read back from the database?", "No", AMBER)
        if st["finished"]:
            badge(d, 0, 900, "Done · save not read back", AMBER)

    def middle(canvas, d, t_agent_s, compiled_alpha):
        st = agent_state(data, t_agent_s)
        panel_header(
            d,
            1,
            "2 · OPENADAPT",
            "Watches, then compiles",
            "JIT wrapper records the agent's actions",
        )
        x = COL_X[1] + 24
        y = VID_Y
        if compiled_alpha <= 0:
            text(d, (x, y), "Recording", 26, BLUE, True)
            text(
                d,
                (x, y + 40),
                plural(st["actions"], "action") + " captured",
                38,
                INK,
                True,
            )
            text(
                d,
                (x, y + 96),
                "each with its screen, target, and the claims",
                20,
                MUTED,
            )
            text(d, (x, y + 122), "table before and after", 20, MUTED)
            yy = y + 168
            for a in st["log"][-16:]:
                d.text((x, yy), describe_action(a)[:46], font=mono(18), fill=INK)
                yy += 26
            return
        text(d, (x, y), "Compiled program", 26, GREEN, True)
        text(
            d,
            (x, y + 40),
            f"{len(steps)} steps · {len(params)} parameters",
            34,
            INK,
            True,
        )
        yy = y + 96
        shown = int(len(steps) * min(1.0, compiled_alpha))
        for st_ in steps[:shown]:
            line = f"{st_['id'][-2:]}  {st_['intent']}"
            line = line.replace("recorded visual target", "visual target")
            d.text(
                (x, yy),
                line[:44],
                font=mono(17),
                fill=INK if st_["risk"] == "reversible" else AMBER,
            )
            yy += 23
        yy += 10
        text(d, (x, yy), "Parameters: " + ", ".join(params), 20, MUTED)
        yy += 30
        text(d, (x, yy), f"Record check: {len(checks)} reads of the claims", 20, MUTED)
        text(d, (x, yy + 26), "table (read-only SQL role)", 20, MUTED)
        badge(d, 1, 900, "Demo profile · not admitted for production", AMBER)
        text(
            d,
            (x, 950),
            "The standard gate refuses these openIMIS forms today.",
            18,
            MUTED,
        )

    def right(canvas, d, upto, frame=None, note=None, note_color=GREEN, lapse=None):
        panel_header(
            d,
            2,
            "3 · THE PROGRAM",
            f"Runs the next {n_records} claims",
            "no model · each save read back from the database",
        )
        video_box(canvas, 2, frame, dim=frame is None)
        if frame is None:
            text(
                d,
                (COL_X[2] + COL_W // 2, VID_Y + VID_H // 2 - 14),
                "Waiting for a compiled program",
                26,
                MUTED,
                False,
                "ma",
            )
        bs = batch_state(data, upto)
        counter(d, 2, 0, "Claims done", f"{bs['done']} of {n_records}")
        counter(
            d,
            2,
            1,
            "Time per saved claim (median) · cost",
            (f"{bs['median']:.1f} s · $0.00" if bs["median"] else "— · $0.00"),
        )
        if load_note(data) and bs["median"]:
            text(
                d,
                (COL_X[2] + 24, VID_Y + VID_H + 30 + 2 * 74 + 74),
                load_note(data),
                16,
                MUTED,
            )
        counter(
            d,
            2,
            2,
            "Saved and read back · stopped safely",
            f"{bs['saved']} · {bs['stopped']}",
            GREEN,
        )
        if lapse:
            text(d, (COL_X[2] + COL_W - 24, VID_Y + 12), lapse, 22, INK, True, "ra")
        if note:
            badge(d, 2, 900, note, note_color)

    scenes: list[Scene] = []

    def title(canvas, t):
        d = ImageDraw.Draw(canvas)
        text(d, (W // 2, 430), "One agent run in. A program out.", 72, INK, True, "ma")
        text(
            d,
            (W // 2, 530),
            "openIMIS claim entry · synthetic data · every number measured",
            32,
            MUTED,
            False,
            "ma",
        )

    scenes.append(Scene(3.5 if cut == "full" else 2.0, title))

    def agent_scene(canvas, t):
        d = ImageDraw.Draw(canvas)
        ta = t * agent_speed
        left(canvas, d, ta)
        middle(canvas, d, ta, 0)
        right(canvas, d, 0)
        if agent_speed != 1.0:
            text(
                d,
                (COL_X[0] + COL_W - 24, VID_Y + 12),
                f"time-lapse ×{agent_speed:g}",
                22,
                INK,
                True,
                "ra",
            )

    scenes.append(
        Scene(
            t_agent,
            agent_scene,
            "Claude enters one claim through the API. OpenAdapt records every action.",
        )
    )

    def compile_scene(canvas, t):
        d = ImageDraw.Draw(canvas)
        left(canvas, d, a_total, dim=True)
        middle(canvas, d, a_total, t / 2.5)
        right(canvas, d, 0)

    scenes.append(
        Scene(
            4.0 if cut == "full" else 2.0,
            compile_scene,
            "The recording becomes a program with parameters and a database check.",
        )
    )

    if program_clip is not None and first_ok:
        att = recs[first_ok][-1]
        p_off = program_offset(program_clip, att)
        speed = 1.0 if cut == "full" else 4.0

        def program_scene(canvas, t):
            d = ImageDraw.Draw(canvas)
            left(canvas, d, a_total, dim=True)
            middle(canvas, d, a_total, 1)
            tp = t * speed
            done = tp >= att.program_s
            right(
                canvas,
                d,
                first_ok if done else first_ok - 1,
                program_clip.at(p_off + min(tp, att.program_s)),
                note=(
                    f"Claim {first_ok}: saved, {att.verdicts} by the database"
                    if done
                    else None
                ),
                lapse=None if speed == 1 else f"time-lapse ×{speed:g}",
            )

        scenes.append(
            Scene(
                att.program_s / speed + 2.0,
                program_scene,
                "The program enters the next claim. No model. The save is read back.",
            )
        )

    last_before = (change_rec - 1) if change_rec else n_records
    lapse_s = 8.0 if cut == "full" else 3.0

    def lapse_scene(canvas, t):
        d = ImageDraw.Draw(canvas)
        left(canvas, d, a_total, dim=True)
        middle(canvas, d, a_total, 1)
        start = first_ok or 1
        upto = start + int((last_before - start) * min(1.0, t / (lapse_s - 0.5)))
        speedup = sum(
            recs[r][-1].program_s
            for r in range(start + 1, last_before + 1)
            if r in recs
        ) / max(lapse_s - 0.5, 0.1)
        frame = (
            program_clip.at((t * 4) % program_clip.duration) if program_clip else None
        )
        right(canvas, d, upto, frame, lapse=f"time-lapse ×{speedup:.0f}")

    scenes.append(
        Scene(
            lapse_s,
            lapse_scene,
            "Claims that stop before saving write nothing and are retried or "
            "sent to a person.",
        )
    )

    if change_rec and change_clip is not None:
        att = recs[change_rec][0]
        c_off = program_offset(change_clip, att)
        change_s = att.program_s + 4.0 if cut == "full" else 4.0
        speed = 1.0 if cut == "full" else max(1.0, att.program_s / 2.5)

        def change_scene(canvas, t):
            d = ImageDraw.Draw(canvas)
            left(canvas, d, a_total, dim=True)
            middle(canvas, d, a_total, 1)
            tp = t * speed
            ended = tp >= att.program_s
            note = None
            color = AMBER
            if ended:
                if att.success:
                    note, color = f"Claim {change_rec}: saved, {att.verdicts}", GREEN
                else:
                    note = (
                        f"Claim {change_rec}: stopped before saving · sent to a person"
                    )
            right(
                canvas,
                d,
                change_rec - 1 if not ended else change_rec,
                change_clip.at(c_off + min(tp, att.program_s)),
                note,
                color,
            )
            text(
                d,
                (COL_X[2] + 24, VID_Y - 30),
                f"Claim {change_rec}: the vendor relabels the Save button",
                22,
                AMBER,
                True,
            )

        scenes.append(
            Scene(
                change_s,
                change_scene,
                change_caption(data, att, steps),
            )
        )

    def end(canvas, t):
        d = ImageDraw.Draw(canvas)
        bs = batch_state(data, n_records)
        a = agent_state(data, a_total)
        text(d, (W // 2, 250), "Measured on this run", 40, MUTED, True, "ma")
        rows = [
            (
                "Agent (once)",
                f"{a['total']:.1f} s · {a['calls']} model calls · "
                f"${a['cost']:.3f} · save not read back",
            ),
            (
                "Program",
                f"{bs['median']:.1f} s per saved claim (median) · 0 model calls · $0"
                if bs["median"]
                else "—",
            ),
            (
                "Read back",
                f"{bs['saved']} of {plural(bs['done'], 'claim')} saved and "
                "confirmed by the database",
            ),
            (
                "Stopped",
                f"{plural(bs['stopped'], 'claim')} stopped before saving; "
                "nothing written",
            ),
        ]
        y = 340
        for k, v in rows:
            text(d, (520, y), k, 32, MUTED, True, "ra")
            text(d, (560, y), v, 32, INK)
            y += 70
        if data.paused:
            text(
                d,
                (W // 2, 640),
                f"Claims {data.paused['paused_at_record'] + 1}-{n_records}: paused "
                f"for a person after the UI change",
                28,
                AMBER,
                False,
                "ma",
            )
        text(
            d,
            (W // 2, 700),
            "Demo profile: not admitted for production. The standard gate refuses "
            "these openIMIS forms today.",
            24,
            AMBER,
            False,
            "ma",
        )
        if load_note(data):
            text(
                d,
                (W // 2, 780),
                "Program times " + load_note(data) + ".",
                22,
                MUTED,
                False,
                "ma",
            )
        text(
            d,
            (W // 2, 740),
            "Raw data: demo/cua_to_program/results",
            24,
            MUTED,
            False,
            "ma",
        )

    scenes.append(Scene(6.0 if cut == "full" else 3.5, end))
    return scenes


def render(data: Data, cut: str, out: Path) -> None:
    cache = Path(tempfile.mkdtemp(prefix="oa-render-"))
    try:
        scenes = build(data, cut, cache)
        total = sum(s.seconds for s in scenes)
        proc = subprocess.Popen(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-s",
                f"{W}x{H}",
                "-r",
                str(FPS),
                "-i",
                "-",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-crf",
                "20",
                "-preset",
                "medium",
                "-movflags",
                "+faststart",
                str(out),
            ],
            stdin=subprocess.PIPE,
        )
        assert proc.stdin is not None
        for scene in scenes:
            for i in range(int(round(scene.seconds * FPS))):
                canvas = Image.new("RGB", (W, H), BG)
                scene.draw(canvas, i / FPS)
                if scene.caption:
                    d = ImageDraw.Draw(canvas)
                    text(d, (W // 2, 1022), scene.caption, 26, MUTED, False, "ma")
                proc.stdin.write(canvas.tobytes())
        proc.stdin.close()
        proc.wait()
        print(f"{out} ({total:.1f} s)")
    finally:
        shutil.rmtree(cache, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--cut", choices=["full", "social"], default="full")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    data = load(args.run_id)
    out = args.out or (
        HERE / "results" / args.run_id / f"cua-to-program-{args.cut}.mp4"
    )
    render(data, args.cut, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
