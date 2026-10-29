"""Harness for the real H.264/AVC444 remote-desktop lab. Runs on vm-client.

``python harness.py <job.json> <out_dir>``, started by runner.py inside the
interactive ``harness`` session. Job kinds:

- ``probe``: open the Remote Desktop Connection (mstsc) window for one app,
  capture frames through the shipped ``RemoteDisplayBackend`` +
  ``Win32WindowClient``, and save them with the window geometry.

The backend is the unmodified product code; only the window selection
(process ``mstsc``, exact window title) is configured here.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image

from openadapt_flow.backends.remote_display import RemoteDisplayBackend

APPS = {
    "emr": {"target": "10.30.1.7"},
    "pn": {"target": "vm-rdhost"},
}
DESKTOP = (1280, 800)


def window_title(app: str) -> str:
    return f"{APPS[app]['target']} - Remote Desktop Connection"


def open_session(
    app: str, *, bpp: int = 32, desktop: tuple[int, int] = DESKTOP, wait_s: float = 60.0
) -> None:
    """Start mstsc for ``app`` unless its window already exists."""
    backend = RemoteDisplayBackend(owner_substr="mstsc", title_substr=window_title(app))
    if backend._client.find_windows("mstsc", window_title(app)):  # noqa: SLF001
        return
    # Settings go in Default.rdp, which mstsc reads for /v: connections;
    # opening an unsigned .rdp file would raise a publisher warning instead.
    rdp = Path.home() / "Documents" / "Default.rdp"
    rdp.parent.mkdir(parents=True, exist_ok=True)
    rdp.write_text(
        "\n".join(
            [
                f"desktopwidth:i:{desktop[0]}",
                f"desktopheight:i:{desktop[1]}",
                "screen mode id:i:1",
                "smart sizing:i:0",
                f"session bpp:i:{bpp}",
                "authentication level:i:0",
                "prompt for credentials:i:0",
                "redirectclipboard:i:0",
                "audiomode:i:2",
                "connection type:i:7",
                "networkautodetect:i:1",
                "bandwidthautodetect:i:1",
                "displayconnectionbar:i:0",
                "autoreconnection enabled:i:1",
            ]
        ),
        encoding="utf-16",
    )
    subprocess.Popen(
        ["mstsc", f"/v:{APPS[app]['target']}", f"/w:{desktop[0]}", f"/h:{desktop[1]}"]
    )
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if backend._client.find_windows("mstsc", window_title(app)):  # noqa: SLF001
            return
        time.sleep(1)
    raise TimeoutError(f"mstsc window for {app} did not appear")


def close_sessions() -> None:
    subprocess.run(["taskkill", "/im", "mstsc.exe", "/f"], capture_output=True)


def backend_for(app: str, **kwargs) -> RemoteDisplayBackend:
    return RemoteDisplayBackend(
        owner_substr="mstsc",
        title_substr=window_title(app),
        max_frame_age_s=kwargs.pop("max_frame_age_s", 10.0),
        **kwargs,
    )


def save_frame(png: bytes, out: Path, name: str) -> dict:
    path = out / f"{name}.png"
    path.write_bytes(png)
    w, h = Image.open(io.BytesIO(png)).size
    return {"file": path.name, "sha256": hashlib.sha256(png).hexdigest(), "size": [w, h]}


def probe(job: dict, out: Path) -> dict:
    app = job["app"]
    open_session(app, bpp=job.get("bpp", 32))
    time.sleep(job.get("settle_s", 20))
    backend = backend_for(app)
    frames = []
    for i in range(job.get("frames", 3)):
        frames.append(save_frame(backend.screenshot(), out, f"{app}_probe_{i}"))
        time.sleep(2)
    windows = [
        {"title": w.title, "bounds": list(w.bounds), "pid": w.pid}
        for w in backend._client.find_windows("mstsc", window_title(app))  # noqa: SLF001
    ]
    return {"app": app, "viewport": list(backend.viewport), "windows": windows, "frames": frames}


def diagnose(job: dict, out: Path) -> dict:
    """Full-screen grab plus every visible top-level window and its process."""
    import ctypes
    from ctypes import wintypes

    from PIL import ImageGrab

    if job.get("app"):
        try:
            open_session(job["app"], wait_s=job.get("wait_s", 30))
        except TimeoutError:
            pass
    buf = io.BytesIO()
    ImageGrab.grab(all_screens=True).save(buf, "PNG")
    frame = save_frame(buf.getvalue(), out, "screen")
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    rows: list[dict] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        h = kernel32.OpenProcess(0x1000, False, pid.value)
        if h:
            kernel32.QueryFullProcessImageNameW(h, 0, exe, ctypes.byref(size))
            kernel32.CloseHandle(h)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        texts: list[str] = []
        if cls.value == "#32770":
            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def child(ch, _lp):
                t = ctypes.create_unicode_buffer(1024)
                user32.GetWindowTextW(ch, t, 1024)
                if t.value:
                    texts.append(t.value)
                return True

            user32.EnumChildWindows(hwnd, child, 0)
        rows.append({"title": title.value, "class": cls.value, "exe": exe.value,
                     "rect": [rect.left, rect.top, rect.right, rect.bottom],
                     "texts": texts})
        return True

    user32.EnumWindows(visit, 0)
    return {"screen": frame, "windows": rows}


# --- Patient Notes campaign ---------------------------------------------------

PN_ORACLE = "http://10.30.1.7:8770"
PN_TARGET_ID = 1  # Neil Sorenson
# Window-pixel points on the 1280x800 session, read off a probe frame.
PN_STEPS = {
    "search_box": (260, 55),
    "search_button": (559, 55),
    "first_row": (115, 145),
    "note_box": (315, 400),
    "save_button": (560, 489),
}
# Conditions under which the target stays unambiguous: a stop is a false stop.
PN_MUST_SAVE = {"healthy", "reorder", "decoy", "bpp16", "desktop1440"}


def oracle(base: str, path: str, timeout: float = 60.0):
    import urllib.request

    with urllib.request.urlopen(f"{base}{path}", timeout=timeout) as resp:
        return json.loads(resp.read())


def wait_steady(app: str, timeout_s: float = 45.0) -> None:
    """Wait until the remote window is painted and two frames agree."""
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        frame = backend_for(app).screenshot()
        digest = hashlib.sha256(frame).hexdigest()
        if digest == last:
            return
        last = digest
        time.sleep(3)


def pn_record(app: str, rec_dir: Path) -> None:
    from openadapt_flow.recorder import Recorder

    rec = Recorder(
        backend_for(app), rec_dir,
        settle_interval_s=0.3, settle_stable_frames=2, settle_timeout_s=6.0,
    )
    rec.click(*PN_STEPS["search_box"])
    rec.type_text("Neil")
    rec.click(*PN_STEPS["search_button"])
    rec.click(*PN_STEPS["first_row"])
    rec.click(*PN_STEPS["note_box"])
    rec.type_text("recorded demonstration note", param="note")
    rec.click(*PN_STEPS["save_button"])
    rec.finish()


def step_summary(r) -> dict:
    res = r.resolution
    return {
        "step_id": r.step_id, "intent": r.intent, "ok": r.ok, "skipped": r.skipped,
        "rung": getattr(res.rung, "value", str(res.rung)) if res else None,
        "confidence": round(res.confidence, 3) if res else None,
        "point": list(res.point) if res else None,
        "safety_halt": r.safety_halt,
        "failure_category": getattr(r.failure_category, "value", r.failure_category),
        "detail": (getattr(r, "error", None) or getattr(r, "message", None)),
    }


def classify(db_outcome: str, success: bool, must_save: bool) -> str:
    if db_outcome == "correct":
        return "verified_save" if success else "stopped_after_save"
    if db_outcome == "wrong_target":
        return "silent_wrong" if success else "flagged_wrong"
    if success:
        return "false_completion"
    return "false_stop" if must_save else "stop_on_lookalike"


def zip_dir(src: Path, dest: Path) -> None:
    import zipfile

    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(src))


def run_campaign(job: dict, out: Path, app: str, spec: dict) -> dict:
    """Record once, compile, then replay every condition; judge from the record.

    ``spec``: record(rec_dir), reset(drift), params(token), judge(token) ->
    (db_outcome, detail), must_save (condition names), name.
    """
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.runtime.replayer import Replayer

    work = Path(r"C:\oa\work") / job["id"]
    work.mkdir(parents=True, exist_ok=True)
    spec["reset"]("none")
    open_session(app)
    wait_steady(app)
    save_frame(backend_for(app).screenshot(), out, f"{app}_record_start")
    t0 = time.monotonic()
    spec["record"](work / "rec")
    workflow = compile_recording(work / "rec", work / "bundle", name=spec["name"])
    record_s = time.monotonic() - t0
    zip_dir(work / "bundle", out / f"{app}_bundle.zip")

    trials = []
    session = (32, DESKTOP)
    for cond in job["conditions"]:
        name, drift = cond["name"], cond.get("drift", "none")
        want = (cond.get("bpp", 32), tuple(cond.get("desktop", DESKTOP)))
        for i in range(cond["n"]):
            if want != session:
                close_sessions()
                time.sleep(3)
                open_session(app, bpp=want[0], desktop=want[1])
                session = want
            spec["reset"](drift)
            wait_steady(app)
            token = f"Oa{job['id'][-4:]}{len(trials):03d}{name[:4]}".replace("-", "")
            run_dir = work / f"run_{len(trials):03d}_{name}"
            first = backend_for(app).screenshot()
            t = time.monotonic()
            error, extra = None, {}
            try:
                report = Replayer(backend_for(app), poll_interval_s=0.3).run(
                    workflow, params=spec["params"](token), bundle_dir=work / "bundle",
                    run_dir=run_dir,
                )
                success, model_calls = bool(report.success), report.model_calls
                rungs, terminal = dict(report.rung_counts), report.terminal_outcome
                steps = [step_summary(r) for r in report.results]
                extra = {
                    "execution_outcome": getattr(report.execution_outcome, "value",
                                                 report.execution_outcome),
                    "heal_count": report.heal_count,
                    "halt": report.halt.model_dump(mode="json") if report.halt else None,
                    "render_scale": report.render_scale,
                }
            except Exception as exc:  # noqa: BLE001 -- a crash is a stop, recorded
                success, model_calls, rungs, terminal, steps = False, 0, {}, None, []
                error = repr(exc)
            elapsed = time.monotonic() - t
            db_outcome, detail = spec["judge"](token)
            must_save = name in spec["must_save"]
            frames = sorted(run_dir.rglob("*.png"))
            trial = {
                "trial": len(trials) + 1, "condition": name, "drift": drift,
                "bpp": want[0], "desktop": list(want[1]), "token": token,
                "success": success, "terminal_outcome": terminal, "error": error,
                "model_calls": model_calls, "rung_counts": rungs,
                "steps": steps, **extra, "db_outcome": db_outcome, "record_detail": detail,
                "class": classify(db_outcome, success, must_save), "must_save": must_save,
                "elapsed_s": round(elapsed, 2),
                "first_frame_sha256": hashlib.sha256(first).hexdigest(),
                "frame_sha256": [hashlib.sha256(f.read_bytes()).hexdigest() for f in frames],
            }
            report_path = run_dir / "report.json"
            if report_path.exists():
                (out / f"report_{len(trials):03d}.json").write_bytes(report_path.read_bytes())
            if i == 0:
                trial["first_frame"] = save_frame(first, out, f"{app}_{name}_first")
                zip_dir(run_dir, out / f"{app}_run_{name}.zip")
            trials.append(trial)
            (out / "trials.json").write_text(json.dumps(trials, indent=2), encoding="utf-8")
    return {"app": app, "record_s": round(record_s, 2), "trials": trials}


def pn_campaign(job: dict, out: Path) -> dict:
    def judge(token: str):
        holders = [r["id"] for r in oracle(PN_ORACLE, "/all") if r["note"] == token]
        outcome = (
            "correct" if holders == [PN_TARGET_ID]
            else "wrong_target" if holders else "no_write"
        )
        return outcome, {"note_holders": holders}

    return run_campaign(job, out, "pn", {
        "name": "avc444-patient-notes",
        "record": lambda rec_dir: pn_record("pn", rec_dir),
        "reset": lambda drift: oracle(PN_ORACLE, f"/reset?drift={drift}"),
        "params": lambda token: {"note": token},
        "judge": judge,
        "must_save": PN_MUST_SAVE,
    })


# --- OpenEMR campaign ----------------------------------------------------------

EMR_ORACLE = "http://10.30.1.6:8771"
EMR_LAST = "Avclab"
EMR_DOB = "2001-01-01"
# Window-pixel points on the 1280x800 session, read off drive frames.
EMR_STEPS = {
    "username": (705, 396), "password": (684, 450), "login": (640, 558),
    "patient_menu": (361, 43), "new_search": (378, 71),
    "first_name": (445, 311), "last_name": (706, 311),
    "dob": (429, 423), "sex": (965, 423), "billing_label": (146, 599),
    "create": (272, 733), "confirm": (600, 261),
}
EMR_MUST_SAVE = {"healthy", "bpp16", "desktop1440"}


def emr_record(rec_dir: Path, order: str = "last_first") -> None:
    from openadapt_flow.recorder import Recorder

    rec = Recorder(
        backend_for("emr"), rec_dir,
        settle_interval_s=0.4, settle_stable_frames=2, settle_timeout_s=10.0,
    )
    st = EMR_STEPS
    # Lab fixture account from benchmark/openemr_live/docker-compose.yml;
    # passed as run parameters so no credential is compiled into the bundle.
    rec.click(*st["username"])
    rec.type_text("admin", param="username")
    rec.click(*st["password"])
    rec.type_text("pass", param="password")
    rec.click(*st["login"])
    # The main screen loads for several seconds after login; a click before it
    # paints would record a blank crop as the menu's template.
    time.sleep(12)
    rec.click(*st["patient_menu"])
    rec.click(*st["new_search"])
    time.sleep(6)
    if order == "first_last":
        rec.click(*st["first_name"])
        rec.type_text("Recorded", param="first_name")
        rec.click(*st["last_name"])
        rec.type_text(EMR_LAST)
    else:
        # Fixed last name first: no later click's identity band then holds a
        # value that changes from run to run (see README, "identity band").
        rec.click(*st["last_name"])
        rec.type_text(EMR_LAST)
        rec.click(*st["first_name"])
        rec.type_text("Recorded", param="first_name")
    rec.click(*st["dob"])
    rec.type_text(EMR_DOB)
    rec.press("escape")
    rec.click(*st["sex"])
    rec.type_text("F")
    rec.press("enter")
    rec.click(*st["billing_label"])
    rec.press("end")
    rec.click(*st["create"])
    time.sleep(3)
    rec.click(*st["confirm"])
    rec.finish()


def emr_judge(token: str):
    rows = oracle(EMR_ORACLE, f"/patients?token={token}")
    exact = [r for r in rows if r["fname"] == token and r["lname"] == EMR_LAST
             and r["DOB"] == EMR_DOB and r["sex"] == "Female"]
    if len(exact) == 1 and len(rows) == 1:
        outcome = "correct"
    elif rows:
        outcome = "wrong_target"  # a field landed elsewhere, or a duplicate row
    else:
        outcome = "no_write"
    return outcome, {"rows": rows}


def emr_campaign(job: dict, out: Path) -> dict:
    def reset(_drift: str) -> None:
        oracle(PN_ORACLE, "/edge_restart")
        time.sleep(4)

    try:
        return run_campaign(job, out, "emr", {
            "name": "avc444-openemr-new-patient",
            "record": lambda rec_dir: emr_record(rec_dir, job.get("order", "last_first")),
            "reset": reset,
            "params": lambda token: {"username": "admin", "password": "pass",
                                     "first_name": token},
            "judge": emr_judge,
            "must_save": EMR_MUST_SAVE,
        })
    finally:
        oracle(EMR_ORACLE, "/cleanup?prefix=Oa" + job["id"][-4:].replace("-", ""))
        oracle(EMR_ORACLE, "/cleanup?prefix=Recorded")


def drive(job: dict, out: Path) -> dict:
    """Exploration: run a list of actions through the backend, frame after each.

    Actions: ["click", x, y] | ["type", text] | ["key", name] | ["wait", s]
    | ["oracle", path] (a call to the host reset service).
    """
    app = job["app"]
    open_session(app)
    time.sleep(job.get("settle_s", 3))
    backend = backend_for(app)
    frames = [save_frame(backend.screenshot(), out, f"{app}_drive_00")]
    for i, act in enumerate(job["actions"], start=1):
        verb = act[0]
        if verb == "click":
            backend.click(int(act[1]), int(act[2]))
        elif verb == "type":
            backend.type_text(act[1])
        elif verb == "key":
            backend.press(act[1])
        elif verb == "wait":
            time.sleep(float(act[1]))
        elif verb == "scroll":
            backend.scroll(int(act[1]), int(act[2]))
        elif verb == "oracle":
            oracle(PN_ORACLE, act[1])
        time.sleep(job.get("step_wait_s", 2.0))
        if verb != "wait" or job.get("frame_after_wait"):
            frames.append(save_frame(backend.screenshot(), out, f"{app}_drive_{i:02d}"))
    return {"app": app, "frames": frames}


def main(job_path: str, out_dir: str) -> int:
    job = json.loads(Path(job_path).read_text(encoding="utf-8-sig"))
    out = Path(out_dir)
    if job.get("fresh"):
        close_sessions()
        time.sleep(3)
    kinds = {"probe": probe, "diagnose": diagnose, "pn_campaign": pn_campaign, "emr_campaign": emr_campaign, "drive": drive}
    try:
        result = kinds[job["kind"]](job, out)
        (out / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return 0
    except Exception:
        import traceback

        (out / "error.json").write_text(
            json.dumps({"error": traceback.format_exc()}, indent=2), encoding="utf-8"
        )
        return 1
    finally:
        upload(job, out)


def upload(job: dict, out: Path) -> None:
    """PUT every output file to the job's write-only blob SAS, if given."""
    sas = job.get("upload_sas")
    if not sas:
        return
    import urllib.request

    base, query = sas.split("?", 1)
    for path in sorted(out.iterdir()):
        if not path.is_file():
            continue
        req = urllib.request.Request(
            f"{base}/{job['id']}/{path.name}?{query}",
            data=path.read_bytes(),
            method="PUT",
            headers={"x-ms-blob-type": "BlockBlob"},
        )
        urllib.request.urlopen(req, timeout=120).close()


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
