"""A scripted person demonstrating through the real ``openadapt-flow record``.

The browser is an ordinary Chromium with a loopback DevTools port. The
product's interactive recorder attaches to it (``record --browser-cdp-endpoint``)
exactly as it would to a person's browser; this module then performs the task
with real pointer and keyboard input (``page.mouse`` / ``page.keyboard``),
and ends the recording through ``--until-cmd``.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

FLOW = Path(__file__).resolve().parents[2]
CDP_PORT = 9491
# A person's pace: a pause after each action, and per keystroke.
PACE_S = 2.0
KEY_DELAY_MS = 40


def _free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def chromium_path() -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        return pw.chromium.executable_path


class CdpSession:
    """Chromium with a DevTools port, one page, and Playwright attached."""

    def __init__(self, viewport: tuple[int, int], scale: float, workdir: Path):
        if not _free(CDP_PORT):
            raise RuntimeError(f"DevTools port {CDP_PORT} is busy")
        self.workdir = workdir
        profile = workdir / "chromium-profile"
        shutil.rmtree(profile, ignore_errors=True)
        w, h = viewport
        self.proc = subprocess.Popen(
            [chromium_path(), f"--remote-debugging-port={CDP_PORT}", "--remote-debugging-address=127.0.0.1",
             f"--user-data-dir={profile}", "--headless=new", "--no-first-run",
             "--no-default-browser-check", f"--window-size={w},{h}",
             f"--force-device-scale-factor={scale}", "--ignore-certificate-errors", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        deadline = time.monotonic() + 30
        while True:
            try:
                self.browser = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
                break
            except Exception:  # noqa: BLE001 - still starting
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        ctx = self.browser.contexts[0]
        self.page = ctx.pages[0] if ctx.pages else ctx.new_page()
        self.page.set_viewport_size({"width": w, "height": h})

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{CDP_PORT}"

    def close(self) -> None:
        try:
            self._pw.stop()
        finally:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def record_demo(
    *,
    out: Path,
    origin: str,
    viewport: tuple[int, int],
    scale: float,
    start: Callable[[Any], None],
    perform: Callable[[Any], None],
    params: list[str],
    identifiers: list[str],
    log: Path,
) -> tuple[bool, str, int]:
    """Record one demonstration; returns (ok, error, recorded events)."""
    from benchmark.onboarding_effort.pool.base import _PlainRecorder

    work = out.parent / f".{out.name}-work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    done = work / "done"
    session = CdpSession(viewport, scale, work)
    proc: Optional[subprocess.Popen] = None
    try:
        start(session.page)
        args = ["openadapt-flow", "record", "--backend", "web", "--url", origin, "--browser-cdp-endpoint", session.endpoint,
                "--browser-page-url", session.page.url, "--out", str(out), "--no-encrypt",
                "--until-cmd", f"test -f {done}"]
        for p in params:
            args += ["--param", p]
        for f in identifiers:
            args += ["--identifier", f]
        env = dict(os.environ, PYTHONPATH=str(FLOW), OPENADAPT_NO_LAUNCHER_CHECK="1",
                   PYTHONUNBUFFERED="1")
        with log.open("w") as fh:
            proc = subprocess.Popen(args, cwd=FLOW, env=env, stdout=fh, stderr=subprocess.STDOUT)
        # Wait until the recorder has attached and injected its listeners.
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return False, f"recorder exited early: {log.read_text()[-800:]}", 0
            if _recorder_attached(session.page, log):
                break
            time.sleep(0.5)
        else:
            return False, "recorder did not attach within 120 s", 0
        time.sleep(1.0)
        perform(_PlainRecorder(session.page, pace_s=PACE_S, key_delay_ms=KEY_DELAY_MS))
        time.sleep(2.0)  # let the last action's frames settle
        done.write_text("done")
        proc.wait(timeout=300)
        events = out / "events.jsonl"
        n = sum(1 for _ in events.read_text().splitlines()) if events.exists() else 0
        if proc.returncode != 0 or n == 0:
            return False, f"record exit {proc.returncode}: {log.read_text()[-800:]}", n
        return True, "", n
    except Exception as exc:  # noqa: BLE001 - a failed demonstration is a result
        return False, f"{type(exc).__name__}: {exc}"[:800], 0
    finally:
        if proc is not None and proc.poll() is None:
            done.write_text("done")
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
        session.close()


def _recorder_attached(page: Any, log: Path) -> bool:
    text = log.read_text() if log.exists() else ""
    return "perform your workflow" in text.lower()
