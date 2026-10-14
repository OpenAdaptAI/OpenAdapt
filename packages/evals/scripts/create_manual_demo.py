#!/usr/bin/env python3
"""Create manual demos for DemoLibrary without screenshots.

Generates a DemoLibrary-compatible demo.json from a simple text specification.
Useful when the VM is deallocated or you want to author demos by hand.

The demo.json uses empty screenshot_path fields. The DemoLibrary will fall back
to sequential alignment (step_index) instead of pHash when screenshots are
missing, which is fine for manual demos -- the descriptions carry the guidance.

Usage:
    # Inline steps (pipe-delimited: action_spec | description)
    python scripts/create_manual_demo.py \\
        --task-id custom-notepad-hello \\
        --output demos/custom-notepad-hello \\
        --steps '
        key:win+r | Press Win+R to open Run dialog
        type:notepad | Type notepad in Run dialog
        key:enter | Press Enter to launch Notepad
        click:0.4,0.4 | Click in Notepad text area
        type:Hello World | Type Hello World
        '

    # From a step file (one step per line, same format)
    python scripts/create_manual_demo.py \\
        --task-id custom-notepad-hello \\
        --output demos/custom-notepad-hello \\
        --step-file steps.txt

    # With resolution and description
    python scripts/create_manual_demo.py \\
        --task-id custom-notepad-hello \\
        --output demos/custom-notepad-hello \\
        --resolution 1920x1080 \\
        --description "Open Notepad and type Hello World" \\
        --steps '
        key:win+r | Press Win+R to open Run dialog
        type:notepad | Type notepad
        key:enter | Press Enter
        click:0.4,0.4 | Click in text area
        type:Hello World | Type Hello World
        '

Step specification format:
    action_type:value | Human-readable description

    Supported action types:
        key:KEY_NAME         - Press a key (e.g., key:enter, key:win+r)
        type:TEXT             - Type text (e.g., type:Hello World)
        click:X,Y            - Click at normalized coords (e.g., click:0.5,0.3)
        double_click:X,Y     - Double-click at normalized coords
        scroll:X,Y           - Scroll at position
        wait                 - Wait for UI to update
        done                 - Signal task complete
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def parse_step(line: str, index: int) -> dict:
    """Parse a single step line: 'action_type:value | description'."""
    line = line.strip()
    if not line:
        raise ValueError(f"Empty step line at index {index}")

    if "|" in line:
        action_part, description = line.split("|", 1)
        action_part = action_part.strip()
        description = description.strip()
    else:
        action_part = line.strip()
        description = ""

    # Parse action_type:value
    x, y = None, None
    action_value = ""
    target_description = ""

    if ":" in action_part:
        action_type, value = action_part.split(":", 1)
        action_type = action_type.strip().lower()
        value = value.strip()
    else:
        action_type = action_part.strip().lower()
        value = ""

    if action_type in ("click", "double_click", "right_click", "scroll"):
        if "," in value:
            parts = value.split(",")
            try:
                x = float(parts[0].strip())
                y = float(parts[1].strip())
            except (ValueError, IndexError):
                raise ValueError(
                    f"Step {index}: {action_type} requires X,Y coords, got '{value}'"
                )
        action_desc = f"{action_type.upper()}({x:.2f}, {y:.2f})" if x is not None else action_type.upper()
    elif action_type == "type":
        action_value = value
        action_desc = f"TYPE('{value}')"
    elif action_type == "key":
        action_value = value
        action_desc = f"KEY({value})"
    elif action_type in ("wait", "done"):
        action_desc = f"{action_type.upper()}()"
    else:
        raise ValueError(f"Step {index}: unknown action type '{action_type}'")

    return {
        "step_index": index,
        "screenshot_path": "",
        "action_type": action_type,
        "action_description": action_desc,
        "target_description": target_description,
        "action_value": action_value,
        "x": x,
        "y": y,
        "description": description,
        "metadata": {},
    }


def parse_steps(text: str) -> list[dict]:
    """Parse multi-line step specification."""
    steps = []
    for i, line in enumerate(text.strip().splitlines()):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        steps.append(parse_step(line, len(steps)))
    return steps


def create_demo(
    task_id: str,
    steps: list[dict],
    output_dir: str,
    description: str = "",
    resolution: tuple[int, int] | None = None,
    demo_id: str | None = None,
) -> Path:
    """Create a DemoLibrary-compatible demo.json."""
    if demo_id is None:
        demo_id = "manual"

    out = Path(output_dir) / demo_id
    out.mkdir(parents=True, exist_ok=True)

    metadata: dict = {
        "source": "manual",
        "created_by": "create_manual_demo.py",
    }
    if resolution:
        metadata["resolution"] = {
            "width": resolution[0],
            "height": resolution[1],
        }

    demo = {
        "task_id": task_id,
        "demo_id": demo_id,
        "description": description or f"Manual demo for {task_id}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "metadata": metadata,
        "steps": steps,
    }

    demo_path = out / "demo.json"
    with open(demo_path, "w") as f:
        json.dump(demo, f, indent=2)

    print(f"Created demo: {demo_path}")
    print(f"  Task ID:    {task_id}")
    print(f"  Demo ID:    {demo_id}")
    print(f"  Steps:      {len(steps)}")
    if resolution:
        print(f"  Resolution: {resolution[0]}x{resolution[1]}")
    print()
    for s in steps:
        print(f"  Step {s['step_index']}: {s['action_description']}")
        if s["description"]:
            print(f"         {s['description']}")

    return demo_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create manual demos for DemoLibrary",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--task-id", required=True,
        help="Task identifier (e.g., custom-notepad-hello)",
    )
    parser.add_argument(
        "--output", required=True,
        help="Output directory (e.g., demos/custom-notepad-hello)",
    )
    parser.add_argument(
        "--steps", default=None,
        help="Inline step specification (multi-line string)",
    )
    parser.add_argument(
        "--step-file", default=None,
        help="Path to a file containing step specifications (one per line)",
    )
    parser.add_argument(
        "--description", default="",
        help="Human-readable description of the demo",
    )
    parser.add_argument(
        "--resolution", default=None,
        help="Screen resolution as WIDTHxHEIGHT (e.g., 1280x720)",
    )
    parser.add_argument(
        "--demo-id", default="manual",
        help="Demo ID (default: 'manual')",
    )
    args = parser.parse_args()

    if not args.steps and not args.step_file:
        parser.error("Provide either --steps or --step-file")

    if args.step_file:
        step_text = Path(args.step_file).read_text()
    else:
        step_text = args.steps

    steps = parse_steps(step_text)
    if not steps:
        print("ERROR: No steps parsed.", file=sys.stderr)
        sys.exit(1)

    resolution = None
    if args.resolution:
        parts = args.resolution.lower().split("x")
        if len(parts) != 2:
            parser.error("Resolution must be WIDTHxHEIGHT (e.g., 1280x720)")
        resolution = (int(parts[0]), int(parts[1]))

    create_demo(
        task_id=args.task_id,
        steps=steps,
        output_dir=args.output,
        description=args.description,
        resolution=resolution,
        demo_id=args.demo_id,
    )


if __name__ == "__main__":
    main()
