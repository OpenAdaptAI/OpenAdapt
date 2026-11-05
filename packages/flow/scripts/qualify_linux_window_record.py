#!/usr/bin/env python3
"""Qualify window-scoped ``record --backend linux`` on a real X11 desktop.

Run under Xvfb with a window manager (openbox) and xdotool, as the
flow-linux-atspi CI job does. The check opens two windows of the GTK3 chart
app (tests/native_apps/chart_gtk.py): the target, titled "Chart", and a
second one titled "Other". Both are python3 processes, so only the title
tells them apart. It then runs the real CLI,
``record --backend linux --linux-window-title Chart``, clicks twice in the
target, twice in the other window and once more in the target with xdotool,
and stops the recording with ``--until-cmd``.

It passes only when the recording holds exactly the three target clicks, each
at the point clicked inside the target; every frame has the target
window's size; and meta.json names the target window. It prints one JSON
line of observations and exits non-zero on a mismatch.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

FLOW = Path(__file__).resolve().parents[1]
APP_SOURCE = FLOW / "tests" / "native_apps" / "chart_gtk.py"
TARGET = ("Chart", "rc79284", "MRN RC79284 Okafor, Philip born 1966-02-17")
OTHER = ("Other", "rc48572", "MRN RC48572 Adeyemi, Grace born 1971-03-29")
# Window-relative click points; both windows are moved apart first.
TARGET_ORIGIN = (40, 40)
OTHER_ORIGIN = (620, 40)
TARGET_CLICKS = ((30, 20), (60, 30))
OTHER_CLICKS = ((30, 20), (80, 40))
LAST_TARGET_CLICK = (90, 25)
TOLERANCE_PX = 2


def _xdotool(*args: str) -> str:
    done = subprocess.run(
        ["xdotool", *args], check=True, capture_output=True, text=True, timeout=20
    )
    return done.stdout.strip()


def _window_id(title: str) -> int:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            ids = _xdotool("search", "--onlyvisible", "--name", f"^{title}$").split()
        except subprocess.CalledProcessError:
            ids = []
        if len(ids) == 1:
            return int(ids[0])
        time.sleep(0.3)
    raise SystemExit(f"no single visible window titled {title!r}")


def _size(window: int) -> tuple[int, int]:
    values: dict[str, int] = {}
    for line in _xdotool("getwindowgeometry", "--shell", str(window)).splitlines():
        key, _, value = line.partition("=")
        values[key] = int(value)
    return values["WIDTH"], values["HEIGHT"]


def _click(window: int, rel: tuple[int, int]) -> tuple[int, int]:
    """Activate ``window`` and click at ``rel`` inside it; return the screen point."""
    _xdotool("windowactivate", "--sync", str(window))
    time.sleep(0.4)
    # Relative to the window itself (XTranslateCoordinates): the X/Y that
    # getwindowgeometry prints count the window manager's frame twice.
    _xdotool("mousemove", "--window", str(window), "--sync", str(rel[0]), str(rel[1]))
    time.sleep(0.2)
    where = dict(
        line.partition("=")[::2]
        for line in _xdotool("getmouselocation", "--shell").splitlines()
    )
    _xdotool("click", "1")
    time.sleep(0.8)
    return int(where["X"]), int(where["Y"])


def _png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")


def _run(root: Path, other_clicks: bool = True) -> tuple[dict, list[str]]:
    failures: list[str] = []
    observed: dict = {}
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("OPENADAPT_", "OAFLOW_"))
    }
    apps = []
    for title, patient, banner in (TARGET, OTHER):
        apps.append(
            subprocess.Popen(
                [
                    sys.executable,
                    str(APP_SOURCE),
                    str(root / f"{patient}.sqlite"),
                    title,
                    patient,
                    banner,
                    "plain",
                ],
                env=env,
            )
        )
    record = None
    try:
        target_id = _window_id(TARGET[0])
        other_id = _window_id(OTHER[0])
        for window, (x, y) in ((target_id, TARGET_ORIGIN), (other_id, OTHER_ORIGIN)):
            _xdotool("windowmove", "--sync", str(window), str(x), str(y))
        time.sleep(0.5)
        target_size = _size(target_id)
        observed["target_size"] = target_size
        observed["other_size"] = _size(other_id)

        out = root / "recording"
        stop = root / "stop"
        record_log = (root / "record.log").open("w")
        record = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "openadapt_flow",
                "record",
                "--backend",
                "linux",
                "--linux-window-title",
                TARGET[0],
                "--out",
                str(out),
                "--task",
                "window-scoped Linux record qualification",
                "--until-cmd",
                f"test -e {stop}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=record_log,
            stderr=subprocess.STDOUT,
            env=env,
        )
        # Wait for capture to come up: it writes its session directory.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and record.poll() is None:
            if (out / ".capture").exists():
                break
            time.sleep(0.3)
        time.sleep(3.0)
        if record.poll() is not None:
            failures.append(f"record exited early with {record.returncode}")
            return observed, failures

        expected: list[tuple[int, int]] = []
        clicks: list[dict] = []
        for rel in TARGET_CLICKS:
            clicks.append({"window": "target", "screen": _click(target_id, rel)})
            expected.append(rel)
        for rel in OTHER_CLICKS if other_clicks else ():
            clicks.append({"window": "other", "screen": _click(other_id, rel)})
        clicks.append(
            {"window": "target", "screen": _click(target_id, LAST_TARGET_CLICK)}
        )
        expected.append(LAST_TARGET_CLICK)
        observed["clicks"] = clicks
        time.sleep(1.0)
        stop.touch()
        try:
            observed["record_exit"] = record.wait(timeout=180)
        except subprocess.TimeoutExpired:
            record.kill()
            failures.append("record did not stop within 180 s")
            return observed, failures
        record_log.close()
        if observed["record_exit"] != 0:
            failures.append(f"record exited {observed['record_exit']}")
            observed["record_log_tail"] = (root / "record.log").read_text()[-1500:]
            return observed, failures

        events = [
            json.loads(line)
            for line in (out / "events.jsonl").read_text().splitlines()
            if line.strip()
        ]
        recorded = [e for e in events if e.get("kind") in ("click", "double_click")]
        observed["event_kinds"] = [e.get("kind") for e in events]
        observed["recorded_clicks"] = [(e.get("x"), e.get("y")) for e in recorded]
        if len(recorded) != len(expected):
            failures.append(
                f"{len(recorded)} clicks recorded; expected the {len(expected)} target clicks"
            )
        else:
            for event, rel in zip(recorded, expected):
                if (
                    abs(float(event["x"]) - rel[0]) > TOLERANCE_PX
                    or abs(float(event["y"]) - rel[1]) > TOLERANCE_PX
                ):
                    failures.append(
                        f"click at {(event['x'], event['y'])} is not the target point {rel}"
                    )
        sizes = sorted({_png_size(p) for p in (out / "frames").glob("*.png")})
        observed["frame_sizes"] = sizes
        if sizes != [target_size]:
            failures.append(f"frame sizes {sizes} are not the target window's size")
        meta = json.loads((out / "meta.json").read_text())
        meta_text = json.dumps(meta)
        observed["meta_names_target"] = TARGET[0] in meta_text
        observed["meta_names_other"] = OTHER[0] in meta_text
        if not observed["meta_names_target"] or observed["meta_names_other"]:
            failures.append("meta.json does not name only the target window")
        return observed, failures
    finally:
        if record is not None and record.poll() is None:
            record.kill()
        for app in apps:
            app.kill()
            app.wait(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output", type=Path, default=None, help="write the JSON here too"
    )
    parser.add_argument("--keep", action="store_true", help="keep the temp directory")
    parser.add_argument(
        "--skip-other-clicks",
        action="store_true",
        help="click only in the target (isolates Capture's dropped-input path)",
    )
    args = parser.parse_args()
    if shutil.which("xdotool") is None:
        print("xdotool is required", file=sys.stderr)
        return 2
    root = Path(tempfile.mkdtemp(prefix="oa-linux-window-record-"))
    try:
        observed, failures = _run(root, other_clicks=not args.skip_other_clicks)
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)
    observed["passed"] = not failures
    observed["failures"] = failures
    line = json.dumps(observed, sort_keys=True)
    print(line)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(line + "\n")
    for failure in failures:
        print(f"window record check failed: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
