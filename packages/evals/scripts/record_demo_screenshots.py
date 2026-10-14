#!/usr/bin/env python3
"""Record real screenshots for an existing demo by replaying it on a live WAA VM.

Executes each step in a demo against a running WAA server, capturing a
screenshot before and after each action. Updates the demo.json with the real
screenshot paths so that the enrichment script can extract crops and run OCR.

This does NOT modify the demo's actions or structure -- it only adds
screenshots.

Usage:
    # Basic: capture screenshots for a demo
    python scripts/record_demo_screenshots.py \
        --demo-dir demos/custom-clear-chrome-data \
        --server-url http://localhost:5001

    # Specify demo subdirectory
    python scripts/record_demo_screenshots.py \
        --demo-dir demos/custom-clear-chrome-data \
        --demo-id manual \
        --server-url http://localhost:5001

    # Custom delay between steps (seconds)
    python scripts/record_demo_screenshots.py \
        --demo-dir demos/custom-clear-chrome-data \
        --server-url http://localhost:5001 \
        --step-delay 2.0

    # Dry run: show what would be executed without actually doing it
    python scripts/record_demo_screenshots.py \
        --demo-dir demos/custom-clear-chrome-data \
        --server-url http://localhost:5001 \
        --dry-run
"""

from __future__ import annotations

import base64
import json
import logging
import sys
import time
from pathlib import Path

import fire
import requests

# Allow running from repo root without install
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logger = logging.getLogger(__name__)

# Default delay between steps in seconds.
DEFAULT_STEP_DELAY = 1.5

# Screenshot endpoint path on WAA server.
_SCREENSHOT_ENDPOINT = "/screenshot"

# Execute endpoint path on WAA server.
_EXECUTE_ENDPOINT = "/execute"


def _take_screenshot(server_url: str, timeout: float = 30.0) -> bytes | None:
    """Take a screenshot via the WAA server API.

    Returns PNG bytes on success, None on failure.
    """
    url = f"{server_url.rstrip('/')}{_SCREENSHOT_ENDPOINT}"
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()

        # WAA returns {"screenshot": "<base64 PNG>"} or {"image": "<base64>"}
        b64 = data.get("screenshot") or data.get("image")
        if b64:
            return base64.b64decode(b64)

        logger.warning("Screenshot response missing image data: %s",
                       list(data.keys()))
        return None

    except requests.RequestException as exc:
        logger.error("Failed to take screenshot: %s", exc)
        return None


def _execute_action(
    server_url: str,
    action_type: str,
    x: float | None = None,
    y: float | None = None,
    action_value: str = "",
    timeout: float = 30.0,
) -> bool:
    """Execute an action on the WAA server.

    Returns True on success, False on failure.
    """
    url = f"{server_url.rstrip('/')}{_EXECUTE_ENDPOINT}"

    # Build the command based on action type
    if action_type in ("click", "double_click", "right_click"):
        if x is None or y is None:
            logger.error("Click action requires x, y coordinates")
            return False
        # WAA expects pixel coordinates or normalized -- use the
        # python -c wrapper as documented in MEMORY.md
        if action_type == "double_click":
            cmd = (
                f'python -c "'
                f"import pyautogui; "
                f"pyautogui.doubleClick({x}, {y})"
                f'"'
            )
        elif action_type == "right_click":
            cmd = (
                f'python -c "'
                f"import pyautogui; "
                f"pyautogui.rightClick({x}, {y})"
                f'"'
            )
        else:
            cmd = (
                f'python -c "'
                f"import pyautogui; "
                f"pyautogui.click({x}, {y})"
                f'"'
            )
    elif action_type == "key":
        # Translate key names for pyautogui
        key = action_value.replace("+", "', '")
        cmd = (
            f'python -c "'
            f"import pyautogui; "
            f"pyautogui.hotkey('{key}')"
            f'"'
        )
    elif action_type == "type":
        # Escape quotes in the text
        escaped = action_value.replace("\\", "\\\\").replace("'", "\\'")
        cmd = (
            f'python -c "'
            f"import pyautogui; "
            f"pyautogui.typewrite('{escaped}', interval=0.05)"
            f'"'
        )
    elif action_type in ("wait", "done"):
        logger.info("Action type %s requires no execution", action_type)
        return True
    else:
        logger.warning("Unknown action type: %s", action_type)
        return False

    payload = {"command": cmd}
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        result = resp.json()
        if result.get("error"):
            logger.warning("Execute returned error: %s", result["error"])
            return False
        return True
    except requests.RequestException as exc:
        logger.error("Failed to execute action: %s", exc)
        return False


def _check_server(server_url: str) -> bool:
    """Check if the WAA server is reachable."""
    try:
        resp = requests.get(
            f"{server_url.rstrip('/')}/health",
            timeout=10,
        )
        return resp.status_code == 200
    except requests.RequestException:
        # Some WAA servers don't have /health, try screenshot instead
        try:
            resp = requests.get(
                f"{server_url.rstrip('/')}{_SCREENSHOT_ENDPOINT}",
                timeout=10,
            )
            return resp.status_code == 200
        except requests.RequestException:
            return False


def record_screenshots(
    demo_dir: str,
    server_url: str = "http://localhost:5001",
    demo_id: str | None = None,
    step_delay: float = DEFAULT_STEP_DELAY,
    dry_run: bool = False,
    resolution: str = "1920x1080",
) -> None:
    """Replay a demo on a live WAA VM, capturing screenshots at each step.

    Args:
        demo_dir: Path to the demo directory.
        server_url: WAA server URL (must be reachable).
        demo_id: Optional demo subdirectory name.
        step_delay: Seconds to wait between steps for UI to settle.
        dry_run: If True, show planned actions without executing.
        resolution: Screen resolution as WIDTHxHEIGHT for coordinate conversion.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    demo_path = Path(demo_dir)
    if not demo_path.exists():
        logger.error("Demo directory does not exist: %s", demo_path)
        sys.exit(1)

    # Parse resolution
    try:
        res_parts = resolution.lower().split("x")
        screen_w, screen_h = int(res_parts[0]), int(res_parts[1])
    except (ValueError, IndexError):
        logger.error("Invalid resolution: %s", resolution)
        sys.exit(1)

    # Find demo.json
    if demo_id:
        demo_json_path = demo_path / demo_id / "demo.json"
    elif (demo_path / "demo.json").exists():
        demo_json_path = demo_path / "demo.json"
    else:
        candidates = list(demo_path.glob("*/demo.json"))
        if not candidates:
            logger.error("No demo.json found in %s", demo_path)
            sys.exit(1)
        demo_json_path = candidates[0]
        logger.info("Found demo.json at: %s", demo_json_path)

    demo_json_dir = demo_json_path.parent

    # Load demo
    with open(demo_json_path) as f:
        demo_data = json.load(f)

    steps = demo_data.get("steps", [])
    if not steps:
        logger.warning("Demo has no steps: %s", demo_json_path)
        return

    # Dry run mode
    if dry_run:
        print(f"\nDRY RUN: {demo_json_path}")
        print(f"  Server:     {server_url}")
        print(f"  Steps:      {len(steps)}")
        print(f"  Resolution: {screen_w}x{screen_h}")
        print(f"  Step delay: {step_delay}s")
        print()
        for i, step in enumerate(steps):
            action = step.get("action_description", "unknown")
            desc = step.get("description", "")
            print(f"  Step {i}: {action}")
            if desc:
                print(f"         {desc}")
            print(f"         -> screenshot before: step_{i:03d}_before.png")
            print(f"         -> screenshot after:  step_{i:03d}_after.png")
        return

    # Check server connectivity
    print(f"\nChecking WAA server at {server_url}...")
    if not _check_server(server_url):
        logger.error(
            "WAA server not reachable at %s. Is the VM running with SSH "
            "tunnel active?",
            server_url,
        )
        sys.exit(1)
    print("  Server is reachable.\n")

    screenshot_count = 0
    error_count = 0

    for i, step in enumerate(steps):
        action_type = step.get("action_type", "")
        x_norm = step.get("x")
        y_norm = step.get("y")
        action_value = step.get("action_value", "")
        desc = step.get("description", "")

        print(f"Step {i}/{len(steps) - 1}: {step.get('action_description', '')}")
        if desc:
            print(f"  Description: {desc}")

        # 1. Take screenshot BEFORE the action
        before_filename = f"step_{i:03d}_before.png"
        before_path = demo_json_dir / before_filename
        print("  Taking screenshot (before)...")
        screenshot_bytes = _take_screenshot(server_url)
        if screenshot_bytes:
            before_path.write_bytes(screenshot_bytes)
            step["screenshot_path"] = before_filename
            screenshot_count += 1
            print(f"  Saved: {before_filename} ({len(screenshot_bytes)} bytes)")
        else:
            logger.warning("  Failed to capture before screenshot for step %d", i)
            error_count += 1

        # 2. Execute the action
        # Convert normalized coordinates to pixel coordinates
        px: float | None = None
        py: float | None = None
        if x_norm is not None and y_norm is not None:
            px = x_norm * screen_w
            py = y_norm * screen_h

        print(f"  Executing: {action_type}({action_value or f'{px},{py}'})")
        success = _execute_action(
            server_url, action_type, x=px, y=py, action_value=action_value
        )
        if not success:
            logger.warning("  Action execution failed for step %d", i)
            error_count += 1

        # 3. Wait for UI to settle
        time.sleep(step_delay)

        # 4. Take screenshot AFTER the action
        after_filename = f"step_{i:03d}_after.png"
        after_path = demo_json_dir / after_filename
        print("  Taking screenshot (after)...")
        screenshot_bytes = _take_screenshot(server_url)
        if screenshot_bytes:
            after_path.write_bytes(screenshot_bytes)
            screenshot_count += 1
            print(f"  Saved: {after_filename} ({len(screenshot_bytes)} bytes)")

            # Store after-screenshot path in metadata for enrichment
            if "metadata" not in step:
                step["metadata"] = {}
            step["metadata"]["screenshot_after_path"] = after_filename
        else:
            logger.warning("  Failed to capture after screenshot for step %d", i)
            error_count += 1

        print()

    # Save updated demo with screenshot paths
    with open(demo_json_path, "w") as f:
        json.dump(demo_data, f, indent=2)

    print(f"Recording complete: {demo_json_path}")
    print(f"  Screenshots captured: {screenshot_count}")
    print(f"  Errors:              {error_count}")
    print(f"  Steps:               {len(steps)}")

    if error_count > 0:
        print(
            f"\n  WARNING: {error_count} errors occurred. Some screenshots "
            "may be missing."
        )

    print(
        "\nNext step: run enrich_demo_targets.py to populate GroundingTarget "
        "data from the captured screenshots."
    )


if __name__ == "__main__":
    fire.Fire(record_screenshots)
