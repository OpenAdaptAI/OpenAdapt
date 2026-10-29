"""Job runner for the real H.264/AVC444 remote-desktop lab (vm-client).

Runs at logon in the ``harness`` user's interactive desktop, which is where
mstsc paints and where SendInput and PrintWindow work. The operator drops a
job file into ``C:\\oa\\jobs`` through an Azure run command; the runner
executes it with ``harness.py`` and writes results to
``C:\\oa\\results\\<job id>``. Run commands themselves execute in session 0,
which has no desktop, so they never drive the client directly.

On first start it stores the two session credentials with cmdkey and
deletes the file they arrived in.
"""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import time
import traceback
import winreg
from pathlib import Path

ROOT = Path(r"C:\oa")
JOBS = ROOT / "jobs"
RESULTS = ROOT / "results"
CREDS = ROOT / "secret" / "creds.json"
HERE = Path(__file__).resolve().parent

# One cmdkey target per session user, so no two users share a credential:
# OpenEMR sessions connect by IP, Patient Notes by host name, the read-back
# session by the fully qualified host name.
READER_TARGET = "vm-rdhost.x3hkkcl3scte1kfhx4qhkg0tta.bx.internal.cloudapp.net"
TARGETS = {
    "emr": ("10.30.1.7", "emruser"),
    "pn": ("vm-rdhost", "pnuser"),
    # Read-back: its own logon, under the host's fully qualified name.
    "pnread": (READER_TARGET, "pnreader"),
}


def _set_resolution(width: int = 1920, height: int = 1080) -> None:
    class DEVMODE(ctypes.Structure):
        _fields_ = [
            ("dmDeviceName", ctypes.c_wchar * 32),
            ("dmSpecVersion", ctypes.c_ushort),
            ("dmDriverVersion", ctypes.c_ushort),
            ("dmSize", ctypes.c_ushort),
            ("dmDriverExtra", ctypes.c_ushort),
            ("dmFields", ctypes.c_ulong),
            ("dmPosition", ctypes.c_long * 2),
            ("dmDisplayOrientation", ctypes.c_ulong),
            ("dmDisplayFixedOutput", ctypes.c_ulong),
            ("dmColor", ctypes.c_short),
            ("dmDuplex", ctypes.c_short),
            ("dmYResolution", ctypes.c_short),
            ("dmTTOption", ctypes.c_short),
            ("dmCollate", ctypes.c_short),
            ("dmFormName", ctypes.c_wchar * 32),
            ("dmLogPixels", ctypes.c_ushort),
            ("dmBitsPerPel", ctypes.c_ulong),
            ("dmPelsWidth", ctypes.c_ulong),
            ("dmPelsHeight", ctypes.c_ulong),
            ("dmDisplayFlags", ctypes.c_ulong),
            ("dmDisplayFrequency", ctypes.c_ulong),
            ("dmICMMethod", ctypes.c_ulong),
            ("dmICMIntent", ctypes.c_ulong),
            ("dmMediaType", ctypes.c_ulong),
            ("dmDitherType", ctypes.c_ulong),
            ("dmReserved1", ctypes.c_ulong),
            ("dmReserved2", ctypes.c_ulong),
            ("dmPanningWidth", ctypes.c_ulong),
            ("dmPanningHeight", ctypes.c_ulong),
        ]

    dm = DEVMODE()
    dm.dmSize = ctypes.sizeof(DEVMODE)
    dm.dmPelsWidth, dm.dmPelsHeight = width, height
    dm.dmFields = 0x00080000 | 0x00100000  # DM_PELSWIDTH | DM_PELSHEIGHT
    ctypes.windll.user32.ChangeDisplaySettingsW(ctypes.byref(dm), 0)


def store_credentials() -> None:
    """Store any session passwords left in the secret file, then delete it."""
    if CREDS.exists():
        creds = json.loads(CREDS.read_text(encoding="utf-8-sig"))
        for target, user in TARGETS.values():
            if user not in creds:
                continue
            subprocess.run(
                ["cmdkey", f"/generic:TERMSRV/{target}", f"/user:vm-rdhost\\{user}",
                 f"/pass:{creds[user]}"],
                check=True, capture_output=True,
            )
        CREDS.unlink()


def _prepare_client() -> None:
    # Self-signed host certificate: connect without the warning dialog.
    key = winreg.CreateKey(
        winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Terminal Server Client"
    )
    winreg.SetValueEx(key, "AuthenticationLevelOverride", 0, winreg.REG_DWORD, 0)
    store_credentials()


def main() -> int:
    JOBS.mkdir(parents=True, exist_ok=True)
    RESULTS.mkdir(parents=True, exist_ok=True)
    _set_resolution()
    _prepare_client()
    (RESULTS / "runner_ready.json").write_text(
        json.dumps({"ready_at": time.time(), "python": sys.version}), encoding="utf-8"
    )
    while True:
        for job_path in sorted(JOBS.glob("*.json")):
            job = json.loads(job_path.read_text(encoding="utf-8-sig"))
            out = RESULTS / job["id"]
            out.mkdir(parents=True, exist_ok=True)
            running = job_path.with_suffix(".running")
            job_path.rename(running)
            log = (out / "runner.log").open("w", encoding="utf-8")
            try:
                proc = subprocess.run(
                    [sys.executable, str(HERE / "harness.py"), str(running), str(out)],
                    stdout=log, stderr=subprocess.STDOUT,
                    timeout=job.get("timeout_s", 7200),
                )
                status = {"exit": proc.returncode}
            except Exception:  # noqa: BLE001 -- the runner must outlive any job
                status = {"exit": -1, "error": traceback.format_exc()}
            log.close()
            (out / "status.json").write_text(json.dumps(status), encoding="utf-8")
            running.rename(job_path.with_suffix(".done"))
        time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(main())
