"""Real-VM probe of WindowsBackend.owned_window_over (opt-in, lab only).

Patient Notes in the lab VM, four cases:

1. no dialog: nothing of the app's is over the region -> None;
2. the app's own "Session notice" modal (interrupt=after_search) -> its
   title, its window and its owner;
3. Notepad pinned topmost over the app -> None (a foreign occluder);
4. a capture taken while Notepad is in front is refused.

Usage: python scripts/desktop/probe_owned_window.py --flow <packages/flow of
the branch under test> --out result.json (lab .env loaded, lease held).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--flow", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.flow))
    sys.path.insert(1, str(HERE))
    import qualify_lab_native as native  # noqa: PLC0415
    import winrm_lab  # noqa: PLC0415

    from openadapt_flow.backends.windows_backend import WindowsBackend  # noqa: PLC0415
    from openadapt_flow.ir import StructuralLocator  # noqa: PLC0415

    assert hasattr(WindowsBackend, "owned_window_over"), "wrong openadapt_flow"
    winrm_lab.load_env_file(Path.home() / "oa/src/OpenAdapt/.env")
    lab = winrm_lab.WinRMLab()
    for name in ("pn_db.py", "patient_notes.ps1", "foreign_window.ps1"):
        lab.push_file(HERE / name, rf"{native.GUEST}\{name}")
    endpoint = lab.launch_agent(
        token=os.environ["OAFLOW_WINLAB_AGENT_TOKEN"],
        server_path=args.flow / "openadapt_flow/backends/win_agent/server.py",
    )
    backend = WindowsBackend(
        endpoint.url,
        auth_token=endpoint.token,
        pin_fingerprint=endpoint.pin_fingerprint,
    )
    result: dict = {}
    native._fresh_app(lab, interrupt="after_search")
    search = native._ready(backend)
    note = native._locate(backend, StructuralLocator(automation_id="noteBox"))
    region = (int(note.point[0]) - 40, int(note.point[1]) - 20, 80, 40)
    result["capture"] = backend.record_verified_window("step_000", *search)
    result["no_dialog"] = backend.owned_window_over(region, pinned_step_id="step_000")

    backend.click(*search)
    backend.type_text("Neil")
    backend.press("Enter")
    time.sleep(3)
    native._save_png(backend, args.out.parent / "owned-dialog.png")
    # The dialog is centered on the maximized form: ask over the screen center
    # (an earlier region only overlapped it, off its center, and was rightly
    # answered None).
    width, height = backend.viewport
    dialog_region = (width // 2 - 40, height // 2 - 20, 80, 40)
    result["dialog_region"] = list(dialog_region)
    result["owned_dialog"] = backend.owned_window_over(
        dialog_region, pinned_step_id="step_000"
    )
    backend.press("Enter")  # close the notice
    time.sleep(1)

    lab.launch_interactive(
        "powershell.exe -ExecutionPolicy Bypass -WindowStyle Hidden "
        f"-File {native.GUEST}\\foreign_window.ps1",
        name="OAFlowForeign",
    )
    time.sleep(10)
    native._save_png(backend, args.out.parent / "foreign-window.png")
    result["foreign_window"] = backend.owned_window_over(
        dialog_region, pinned_step_id="step_000"
    )
    result["capture_while_foreign_in_front"] = backend.record_verified_window(
        "step_001", *search
    )
    lab.stop_interactive("OAFlowForeign")
    lab.run_ps(
        "Stop-Process -Name notepad -Force -ErrorAction SilentlyContinue", check=False
    )
    result["accepted"] = bool(
        result["capture"]
        and result["no_dialog"] is None
        and result["owned_dialog"]
        and result["owned_dialog"].get("title") == "Session notice"
        and result["foreign_window"] is None
        and result["capture_while_foreign_in_front"] is False
    )
    args.out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
