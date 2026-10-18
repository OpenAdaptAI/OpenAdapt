#!/usr/bin/env python3
"""Prove a stable multi-monitor topology on an interactive qualification rig."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Callable

from openadapt_capture.desktop_capture import DesktopCaptureScope


class DisplayTopologyError(RuntimeError):
    """The qualification display topology does not meet the contract."""


def qualify_topology(
    read_snapshot: Callable[[], dict[str, Any]],
    *,
    minimum_monitors: int,
    samples: int,
    interval_seconds: float,
) -> dict[str, Any]:
    if minimum_monitors < 1:
        raise DisplayTopologyError("minimum_monitors must be positive")
    if samples < 2:
        raise DisplayTopologyError("samples must be at least two")

    snapshots: list[dict[str, Any]] = []
    for index in range(samples):
        snapshot = read_snapshot()
        if snapshot.get("coordinate_space") != "virtual_desktop_pixels":
            raise DisplayTopologyError("the coordinate space is not virtual_desktop_pixels")
        monitor_count = snapshot.get("monitor_count")
        monitors = snapshot.get("monitors")
        if (
            isinstance(monitor_count, bool)
            or not isinstance(monitor_count, int)
            or monitor_count < minimum_monitors
        ):
            raise DisplayTopologyError(
                f"the rig has {monitor_count!r} monitors; at least {minimum_monitors} are required"
            )
        if not isinstance(monitors, list) or len(monitors) != monitor_count:
            raise DisplayTopologyError("the monitor inventory does not match monitor_count")
        snapshots.append(snapshot)
        if index + 1 < samples:
            time.sleep(interval_seconds)

    if any(snapshot != snapshots[0] for snapshot in snapshots[1:]):
        raise DisplayTopologyError("the display topology changed during qualification")
    return {
        "schema_version": 1,
        "samples": samples,
        "stable": True,
        "topology": snapshots[0],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minimum-monitors", type=int, default=2)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    evidence = qualify_topology(
        lambda: DesktopCaptureScope.current().snapshot(),
        minimum_monitors=args.minimum_monitors,
        samples=args.samples,
        interval_seconds=args.interval_seconds,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    main()
