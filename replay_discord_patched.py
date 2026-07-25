#!/usr/bin/env python3
"""Replay a macOS Discord bundle with a Dock-safe hit-test patch.

OpenAdapt refuses clicks when window_id_at_point() returns the Dock's
full-screen layer-20 window instead of Discord. This patches the hit-test to
only consider normal app windows (layer 0).
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Optional

from openadapt_flow.backends.macos_backend import MacOSBackend
from openadapt_flow.backends.remote_display import MacWindowClient
from openadapt_flow.ir import Workflow
from openadapt_flow.__main__ import _build_and_run_replayer


def _patch_hit_test() -> None:
    def window_id_at_point_layer0(self, x: float, y: float) -> Optional[int]:
        try:
            import Quartz

            opts = (
                Quartz.kCGWindowListOptionOnScreenOnly
                | Quartz.kCGWindowListExcludeDesktopElements
            )
            wins = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID)
            for window in wins or []:
                if int(window.get("kCGWindowLayer", 0) or 0) != 0:
                    continue
                bounds = window.get("kCGWindowBounds", {}) or {}
                left = float(bounds.get("X", 0.0))
                top = float(bounds.get("Y", 0.0))
                width = float(bounds.get("Width", 0.0))
                height = float(bounds.get("Height", 0.0))
                if width <= 0 or height <= 0:
                    continue
                if left <= x < left + width and top <= y < top + height:
                    window_id = int(window.get("kCGWindowNumber", 0) or 0)
                    return window_id if window_id > 0 else None
        except Exception:
            return None
        return None

    MacWindowClient.window_id_at_point = window_id_at_point_layer0  # type: ignore[method-assign]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--bundle", default="app-run/bundle")
    p.add_argument("--run-dir", default="app-run/run-patched")
    p.add_argument("--macos-app", default="Discord")
    p.add_argument("--macos-window-title", default=None)
    args = p.parse_args()

    _patch_hit_test()

    client = MacWindowClient()
    title = args.macos_window_title
    if not title:
        wins = [
            w
            for w in client.find_windows(args.macos_app, None)
            if w.title and w.on_screen
        ]
        if not wins:
            print(f"No on-screen window for {args.macos_app!r}", file=sys.stderr)
            return 2
        title = wins[0].title

    bundle = Path(args.bundle)
    run_dir = Path(args.run_dir)
    if run_dir.exists():
        shutil.rmtree(run_dir)

    print(f"Replaying {bundle} → {run_dir}")
    print(f"  window title: {title!r} (Dock hit-test patch ON)")

    report = _build_and_run_replayer(
        MacOSBackend(app=args.macos_app, window_title=title),
        workflow=Workflow.load(bundle),
        params={},
        worklists={},
        bundle=bundle,
        run_dir=run_dir,
        save_healed_to=None,
        allow_egress=False,
        effect_verifier=None,
        api_actuator=None,
        durable=False,
        use_structural=True,
    )
    print("success=" + str(bool(getattr(report, "success", False))))
    print(f"report: {run_dir / 'report.json'}")
    return 0 if getattr(report, "success", False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
