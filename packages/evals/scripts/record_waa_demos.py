#!/usr/bin/env python3
"""Record WAA task demos with guided workflow.

This script guides you through recording demos for WAA benchmark tasks.
Supports local recording (via openadapt-capture), remote interactive recording
(via WAA API + VNC), VLM annotation, and demo-conditioned evaluation.

Usage:
    # Local recording (original workflow)
    python record_waa_demos.py record

    # Interactive recording via WAA API (user performs actions on VNC)
    python record_waa_demos.py record-waa \
      --tasks 04d9aeaf,0a0faba3 \
      --server http://localhost:5001 \
      --output waa_recordings/

    # VLM annotation of recorded screenshots
    python record_waa_demos.py annotate \
      --recordings waa_recordings/ \
      --output annotated_demos/ \
      --provider openai

    # Run demo-conditioned eval (wraps eval-suite)
    python record_waa_demos.py eval \
      --demo_dir annotated_demos/ \
      --tasks 04d9aeaf,0a0faba3 \
      --agent api-openai
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Load .env for API keys. Must precede the openadapt_evals imports below,
# which read provider credentials from the environment at import time.
from dotenv import load_dotenv

load_dotenv()

from openadapt_evals.constants import HARDER_TASK_IDS
from openadapt_evals.telemetry import (
    track_demo_recorded,
)

# WAA tasks to record
TASKS = [
    {
        "id": "366de66e-cbae-4d72-b042-26390db2b145-WOS",
        "instruction": (
            "Open Notepad, create a new file named 'draft.txt', "
            "type 'This is a draft.', and save it to the Documents folder."
        ),
        "domain": "notepad",
        "tips": [
            "Click Start menu or press Windows key",
            "Type 'notepad' and click the app",
            "Type the text",
            "Press Ctrl+S to save",
            "Navigate to Documents folder",
            "Name it 'draft.txt' and click Save",
        ],
    },
    {
        "id": "37e10fc4-b4c5-4b02-a65c-bfae8bc51d3f-wos",
        "instruction": "Turn off notifications for my system in the settings.",
        "domain": "settings",
        "tips": [
            "Click Start menu",
            "Click Settings (gear icon)",
            "Click 'System' in the sidebar",
            "Click 'Notifications'",
            "Toggle the main 'Notifications' switch to Off",
        ],
    },
    {
        "id": "0c9dda13-428c-492b-900b-f48562111f93-WOS",
        "instruction": (
            "Create a new folder named 'Archive' in the Documents folder "
            "and move all .docx files into it."
        ),
        "domain": "file_explorer",
        "setup": "create_docx_files",
        "tips": [
            "Open File Explorer (Win+E)",
            "Navigate to Documents",
            "Right-click → New → Folder, name it 'Archive'",
            "Find .docx files (use search or sort by type)",
            "Select all .docx files (Ctrl+click or Ctrl+A after filtering)",
            "Cut (Ctrl+X), open Archive folder, Paste (Ctrl+V)",
        ],
    },
]

# File names for the docx setup task
DOCX_FILES = ["report.docx", "meeting_notes.docx", "proposal.docx"]


def print_header(text: str) -> None:
    """Print a styled header."""
    width = max(60, len(text) + 4)
    print()
    print("=" * width)
    print(f"  {text}")
    print("=" * width)
    print()


CHECKPOINT_FILENAME = "checkpoint.json"


def _save_checkpoint(
    task_dir: Path,
    task_id: str,
    instruction: str,
    completed_steps: list[str],
    remaining_steps: list[str],
    step_plans: list[dict],
    refined_indices: set[int],
    steps_meta: list[dict],
    step_idx: int,
) -> None:
    """Save recording session state to a checkpoint file.

    Called after each step completes so progress survives any failure.
    """
    checkpoint = {
        "task_id": task_id,
        "instruction": instruction,
        "completed_steps": completed_steps,
        "remaining_steps": remaining_steps,
        "step_plans": step_plans,
        "refined_indices": sorted(refined_indices),
        "steps_meta": steps_meta,
        "step_idx": step_idx,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    checkpoint_path = task_dir / CHECKPOINT_FILENAME
    checkpoint_path.write_text(
        json.dumps(checkpoint, indent=2), encoding="utf-8"
    )


def _load_checkpoint(task_dir: Path) -> dict | None:
    """Load a checkpoint file if it exists and is valid JSON.

    Returns the parsed checkpoint dict, or None if no checkpoint exists.
    """
    checkpoint_path = task_dir / CHECKPOINT_FILENAME
    if not checkpoint_path.exists():
        return None
    try:
        data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        # Sanity check: must have essential keys
        required = {
            "task_id", "completed_steps", "remaining_steps",
            "step_plans", "steps_meta", "step_idx",
        }
        if not required.issubset(data.keys()):
            return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


def _delete_checkpoint(task_dir: Path) -> None:
    """Delete the checkpoint file after successful completion."""
    checkpoint_path = task_dir / CHECKPOINT_FILENAME
    if checkpoint_path.exists():
        checkpoint_path.unlink()


def print_task(task: dict, index: int, total: int) -> None:
    """Print task details."""
    print(f"Task {index + 1} of {total}: [{task['domain']}]")
    print()
    print(f"  \"{task['instruction']}\"")
    print()
    print("  Steps:")
    for i, tip in enumerate(task["tips"], 1):
        print(f"    {i}. {tip}")
    print()


def check_dependencies() -> bool:
    """Check and install required dependencies."""
    print("Checking dependencies...")

    # Check openadapt-capture
    try:
        import openadapt_capture

        # Report the resolved path: the import is the availability probe, and
        # naming the file distinguishes a site install from a local checkout.
        print(f"  ✓ openadapt-capture installed ({openadapt_capture.__file__})")
    except ImportError:
        print("  Installing openadapt-capture...")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "openadapt-capture"],
                check=True,
                capture_output=True,
            )
            print("  ✓ openadapt-capture installed")
        except subprocess.CalledProcessError:
            print("  ✗ Failed to install openadapt-capture")
            return False

    # Check magic-wormhole
    import shutil
    if shutil.which("wormhole"):
        print("  ✓ magic-wormhole installed")
    else:
        print("  Installing magic-wormhole...")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "magic-wormhole"],
                check=True,
                capture_output=True,
            )
            print("  ✓ magic-wormhole installed")
        except subprocess.CalledProcessError:
            print("  ✗ Failed to install magic-wormhole")
            return False

    print()
    return True


def setup_create_docx_files() -> None:
    """Create dummy .docx files in Documents for the archive task."""
    docs_dir = Path.home() / "Documents"
    docs_dir.mkdir(parents=True, exist_ok=True)

    # Remove Archive folder from previous runs
    archive_dir = docs_dir / "Archive"
    if archive_dir.exists():
        import shutil
        shutil.rmtree(archive_dir)

    created = []
    for name in DOCX_FILES:
        path = docs_dir / name
        if not path.exists():
            path.write_bytes(b"")
            created.append(name)

    if created:
        print(f"  Created {len(created)} .docx file(s) in Documents: {', '.join(created)}")
    else:
        print("  .docx files already exist in Documents")


SETUP_FUNCTIONS = {
    "create_docx_files": setup_create_docx_files,
}


def get_recordings_dir() -> Path:
    """Get the recordings directory path."""
    # Use a standard location
    home = Path.home()
    recordings_dir = home / ".openadapt" / "waa_demos"
    recordings_dir.mkdir(parents=True, exist_ok=True)
    return recordings_dir


def record_task(task: dict, recordings_dir: Path) -> Path | None:
    """Record a single task.

    Returns:
        Path to recording directory if successful, None otherwise.
    """
    from openadapt_capture import Recorder

    recording_path = recordings_dir / task["id"]

    # Remove existing recording if any
    if recording_path.exists():
        import shutil
        shutil.rmtree(recording_path)

    print("Press ENTER when ready to start recording...")
    print("(Press Ctrl 3 times to stop recording)")
    input()

    print()
    print("🔴 RECORDING... (press Ctrl 3 times to stop)")
    print()

    try:
        with Recorder(
            recording_path,
            task_description=task["instruction"],
            capture_video=True,
            capture_audio=False,
        ) as recorder:
            recorder.wait_for_ready()
            try:
                while recorder.is_recording:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass

        print()
        print(f"✓ Recorded {recorder.event_count} events")
        return recording_path

    except Exception as e:
        print(f"✗ Recording failed: {e}")
        return None


def send_all_recordings(recording_paths: list[Path]) -> None:
    """Send all recordings via wormhole sequentially.

    Each send blocks until the receiver accepts, so this prints clear
    instructions before each one.
    """
    from openadapt_capture.share import send

    for i, path in enumerate(recording_paths, 1):
        print(f"--- Recording {i}/{len(recording_paths)}: {path.name} ---")
        print("Run 'wormhole receive <code>' on the receiving machine.")
        print()
        send(str(path))
        print()


def main() -> int:
    """Main entry point."""
    print_header("WAA Demo Recording Tool")

    if not check_dependencies():
        print("Please install dependencies manually and try again.")
        return 1

    recordings_dir = get_recordings_dir()
    print(f"Recordings will be saved to: {recordings_dir}")

    # Track results
    results = {}  # task_id -> recording_path or None

    # Record each task
    task_index = 0
    while task_index < len(TASKS):
        task = TASKS[task_index]

        print_header(f"Task {task_index + 1} of {len(TASKS)}")
        print_task(task, task_index, len(TASKS))

        # Run setup if needed
        setup_name = task.get("setup")
        if setup_name and setup_name in SETUP_FUNCTIONS:
            print("  Setting up environment...")
            SETUP_FUNCTIONS[setup_name]()
            print()

        recording_path = record_task(task, recordings_dir)

        if recording_path:
            print()
            response = input("Keep this recording? [Y/n/r] ").strip().lower()

            if response in ("", "y", "yes"):
                results[task["id"]] = recording_path
                print(f"✓ Task {task_index + 1} saved")
                task_index += 1
            elif response in ("r", "redo"):
                print("Retrying...")
                continue
            else:
                results[task["id"]] = None
                print(f"✗ Task {task_index + 1} skipped")
                task_index += 1
        else:
            response = input("Recording failed. Retry? [Y/n] ").strip().lower()
            if response in ("", "y", "yes"):
                continue
            else:
                results[task["id"]] = None
                task_index += 1

    # Summary
    print_header("Summary")

    successful = [tid for tid, path in results.items() if path is not None]

    for task in TASKS:
        status = "✓ recorded" if results.get(task["id"]) else "✗ skipped"
        print(f"  Task [{task['domain']}]: {status}")

    print()

    if not successful:
        print("No recordings to send.")
        return 0

    # Send recordings
    response = input(f"Send {len(successful)} recording(s) now? [Y/n] ").strip().lower()

    if response not in ("", "y", "yes"):
        print()
        print("Recordings saved locally. To send later:")
        for task_id in successful:
            print(f"  capture share send {recordings_dir / task_id}")
        return 0

    print_header("Sending Recordings")
    print("Each recording will be sent one at a time.")
    print("You must run 'wormhole receive <code>' on the receiving machine for each one.")
    print()

    paths = [results[tid] for tid in successful]
    send_all_recordings(paths)

    print_header("Done!")

    return 0


# ---------------------------------------------------------------------------
# WAA API interactive recording
# ---------------------------------------------------------------------------


_LO_CLEANUP_SCRIPT = r"""
import os, re, shutil

home = os.path.expanduser("~")
lo_user = os.path.join(home, "AppData", "Roaming", "LibreOffice", "4", "user")

# 1. Delete backup/recovery files
backup_dir = os.path.join(lo_user, "backup")
if os.path.exists(backup_dir):
    files = os.listdir(backup_dir)
    if files:
        shutil.rmtree(backup_dir)
        os.makedirs(backup_dir)
        print(f"Cleared {len(files)} backup file(s)")
    else:
        print("Backup dir empty")
else:
    print("No backup dir")

# 2. Modify registrymodifications.xcu
xcu = os.path.join(lo_user, "registrymodifications.xcu")
if os.path.exists(xcu):
    with open(xcu, "r", encoding="utf-8") as f:
        content = f.read()
    changed = False
    if "RecoveryList" in content:
        content = re.sub(
            r'<item oor:path="/org.openoffice.Office.Recovery/RecoveryList">.*?</item>',
            '', content, flags=re.DOTALL
        )
        changed = True
        print("Removed RecoveryList entries")
    autosave_line = '<item oor:path="/org.openoffice.Office.Recovery/AutoSave"><prop oor:name="Enabled" oor:op="fuse"><value>false</value></prop></item>'
    if "AutoSave" not in content:
        content = content.replace("</oor:items>", autosave_line + "\n</oor:items>")
        changed = True
        print("Added AutoSave=false")
    elif ">true<" in content.split("AutoSave")[1].split("</item>")[0]:
        content = re.sub(
            r'<item oor:path="/org.openoffice.Office.Recovery/AutoSave">.*?</item>',
            autosave_line, content, flags=re.DOTALL
        )
        changed = True
        print("Changed AutoSave to false")
    else:
        print("AutoSave already disabled")
    if changed:
        with open(xcu, "w", encoding="utf-8") as f:
            f.write(content)
        print("Updated registrymodifications.xcu")
else:
    print(f"No xcu found at {xcu}")
"""


def _clear_recovery_data(server: str) -> None:
    """Clear LibreOffice recovery/backup data and disable auto-recovery.

    After a QEMU hard reset, LibreOffice leaves behind recovery files that
    trigger a "Document Recovery" dialog on next launch. This function:
    1. Deletes existing backup/recovery files
    2. Removes stale RecoveryList entries from the config
    3. Disables auto-recovery so it won't create recovery data in the future

    WARNING: This is intentional for the recording workflow — there is no
    user work to preserve on these eval VMs.
    """
    import base64

    import requests

    b64 = base64.b64encode(_LO_CLEANUP_SCRIPT.encode()).decode()
    cmd = (
        f"python -c \""
        f"import base64,tempfile,os,subprocess;"
        f"d=base64.b64decode('{b64}');"
        f"p=os.path.join(tempfile.gettempdir(),'lo_cleanup.py');"
        f"open(p,'wb').write(d);"
        f"r=subprocess.run(['python',p],capture_output=True,text=True);"
        f"print(r.stdout);"
        f"print(r.stderr)"
        f"\""
    )

    try:
        resp = requests.post(
            f"{server}/execute",
            json={"command": cmd},
            timeout=15,
        )
        if resp.ok:
            output = resp.json().get("output", "").strip()
            if output:
                for line in output.splitlines():
                    if line.strip():
                        print(f"    {line}")
            print("  Cleared LibreOffice recovery data.")
        else:
            print(f"  WARNING: recovery cleanup returned {resp.status_code}")
    except Exception as e:
        print(f"  WARNING: recovery cleanup failed: {e}")


def _take_screenshot(server: str) -> bytes:
    """Take a screenshot from the WAA server, raising on failure."""
    import requests

    resp = requests.get(f"{server}/screenshot", timeout=30)
    resp.raise_for_status()
    return resp.content


def _compare_screenshots(png_a: bytes, png_b: bytes) -> float:
    """Compare two PNG screenshots and return pixel similarity (0.0–1.0).

    Uses raw RGB pixel comparison.  Returns 1.0 for identical images.
    The 99.5% threshold used by ``_wait_for_stable_screen`` tolerates
    minor differences like the taskbar clock updating (~0.14% of pixels)
    or cursor blink.
    """
    import io

    from PIL import Image

    img_a = Image.open(io.BytesIO(png_a)).convert("RGB")
    img_b = Image.open(io.BytesIO(png_b)).convert("RGB")

    if img_a.size != img_b.size:
        return 0.0

    bytes_a = img_a.tobytes()
    bytes_b = img_b.tobytes()

    if bytes_a == bytes_b:
        return 1.0  # fast path: identical raw bytes

    # Vectorized comparison via numpy (already a transitive dep via open-clip-torch)
    import numpy as np

    arr_a = np.frombuffer(bytes_a, dtype=np.uint8).reshape(-1, 3)
    arr_b = np.frombuffer(bytes_b, dtype=np.uint8).reshape(-1, 3)
    matching = int(np.all(arr_a == arr_b, axis=1).sum())
    return matching / arr_a.shape[0]


def _wait_for_stable_screen(
    server: str,
    poll_interval: float = 2.0,
    stability_timeout: float = 30.0,
    similarity_threshold: float = 0.995,
    required_stable_checks: int = 3,
) -> bytes:
    """Wait for the VM screen to stabilize, then return the screenshot.

    Polls the QEMU framebuffer (free — local HTTP call) until
    ``required_stable_checks`` consecutive screenshot pairs exceed
    ``similarity_threshold``.  With the defaults (3 checks at 2s
    intervals), the screen must be stable for 6 seconds before
    proceeding.

    Args:
        server: WAA server URL (``/screenshot`` endpoint).
        poll_interval: Seconds between screenshots.
        stability_timeout: Maximum seconds to wait.  If exceeded, the
            last screenshot is returned with a warning.
        similarity_threshold: Pixel-match fraction (0.0–1.0).  0.995
            tolerates taskbar clock and cursor blink.
        required_stable_checks: Consecutive stable pairs required.
            With poll_interval=2 and required_stable_checks=3, the
            screen must be unchanged for 6 seconds.

    Returns:
        PNG screenshot bytes of the stable screen.
    """
    prev_png = _take_screenshot(server)
    stable_count = 0
    deadline = time.time() + stability_timeout

    while time.time() < deadline:
        time.sleep(poll_interval)
        curr_png = _take_screenshot(server)

        similarity = _compare_screenshots(prev_png, curr_png)

        if similarity >= similarity_threshold:
            stable_count += 1
            if stable_count >= required_stable_checks:
                elapsed = stability_timeout - (deadline - time.time())
                print(f"    Screen stable after {elapsed:.0f}s "
                      f"({stable_count} checks, {similarity:.4f} similarity)")
                return curr_png
        else:
            if stable_count > 0:
                print(f"    Screen changed ({similarity:.3f}), resetting stability count")
            stable_count = 0

        prev_png = curr_png

    print(f"    WARNING: Screen did not stabilize within {stability_timeout:.0f}s. "
          "Using last screenshot.")
    return prev_png


def _build_setup_desc(task_config: dict) -> str:
    """Build a human-readable description of the task setup actions."""
    parts = []
    for entry in task_config.get("config", []):
        t = entry.get("type", "")
        p = entry.get("parameters", {})
        if t == "download":
            for f in p.get("files", []):
                parts.append(f"  - File downloaded: {f['path']}")
        elif t == "open":
            parts.append(f"  - File opened: {p.get('path', '?')}")
        elif t == "launch":
            parts.append(f"  - App launched: {p.get('command', '?')}")
    return "\n".join(parts) if parts else "  (none)"


def _vlm_call(
    messages: list[dict],
    api_key: str | None = None,
    model: str = "gpt-4.1-mini",
    max_tokens: int = 800,
    *,
    use_council: bool = True,
) -> str:
    """Send a VLM query, optionally using multi-model consilium council.

    When ``use_council=True`` (default), queries multiple LLMs via consilium
    in Stage-1-only mode (skip_review) for fast multi-model consensus.
    Falls back to single-model OpenAI if consilium is unavailable.
    """
    # Extract the text prompt and optional image from the messages
    prompt_text = ""
    image_bytes_list: list[bytes] | None = None
    for msg in messages:
        if isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if block.get("type") == "text":
                    prompt_text = block["text"]
                elif block.get("type") == "image_url":
                    url = block["image_url"]["url"]
                    if url.startswith("data:image/png;base64,"):
                        import base64 as _b64
                        raw = _b64.b64decode(url.split(",", 1)[1])
                        if image_bytes_list is None:
                            image_bytes_list = []
                        image_bytes_list.append(raw)
        elif isinstance(msg.get("content"), str):
            prompt_text = msg["content"]

    if use_council:
        try:
            from consilium import council_query

            result = council_query(
                prompt_text,
                images=image_bytes_list,
                skip_review=True,  # Stage 1 only — fast + cheap
                budget=0.50,
            )
            return result["final_answer"]
        except ImportError:
            print("  (consilium not installed — falling back to single-model)")
        except Exception as e:
            print(f"  (consilium failed: {e} — falling back to single-model)")

    # Fallback: single-model OpenAI call
    if not api_key:
        import os
        api_key = os.environ.get("OPENAI_API_KEY", "")
    import requests as _req

    resp = _req.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "max_tokens": max_tokens, "messages": messages},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _generate_steps(
    screenshot_png: bytes,
    instruction: str,
    task_config: dict,
    target: str = "human",
) -> str:
    """Use a VLM to generate step-by-step instructions from the current screen.

    Uses consilium multi-model council by default for better accuracy.
    Falls back to single-model OpenAI if consilium is unavailable.

    Args:
        target: ``"human"`` (default) optimizes for a person at a VNC screen
            (prefer drag-fill, bulk select, keyboard shortcuts).
            ``"agent"`` optimizes for an AI agent (prefer keyboard shortcuts
            over drag gestures for reliability, but still minimize steps).

    Returns plain-text numbered steps, or a fallback message on error.
    """
    import base64

    b64 = base64.b64encode(screenshot_png).decode()
    setup_desc = _build_setup_desc(task_config)

    if target == "agent":
        efficiency_guidance = (
            "The steps will be executed by an AI automation agent with mouse "
            "and keyboard control. Minimize total steps — use bulk operations:\n"
            "  - Keyboard shortcuts (Ctrl+D to fill down, Ctrl+Shift+End to select)\n"
            "  - Select a range then apply formatting once\n"
            "  - Copy/paste formulas across cells\n"
            "Prefer keyboard shortcuts over drag gestures (the agent may miss "
            "small UI targets like fill handles). Each step = one action."
        )
    else:
        efficiency_guidance = (
            "The steps will be performed by a human via VNC. "
            "Minimize the number of steps — prefer efficient operations like:\n"
            "  - Click-and-drag to fill formulas across cells\n"
            "  - Select a range then apply formatting once\n"
            "  - Copy/paste or Ctrl+D to fill down\n"
            "  - Auto-fill by dragging the cell handle\n"
            "Do NOT list one step per cell when a bulk operation would work."
        )

    prompt = f"""You are helping complete a task on a Windows desktop.

TASK: {instruction}

ENVIRONMENT SETUP (already done automatically):
{setup_desc}

TARGET: {efficiency_guidance}

Look at the screenshot carefully. Output ONLY a numbered list of steps.
Each step = one action (click, type, drag, keyboard shortcut).
Be specific: reference actual UI elements, cell references, and values visible on screen.
Combine actions where possible (bulk select, fill handle, Ctrl+D).
Do NOT include drafts, commentary, or explanations — just the final steps."""

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{b64}",
                        "detail": "high",
                    },
                },
            ],
        }
    ]

    try:
        return _vlm_call(messages)
    except Exception as e:
        return f"(AI step generation failed: {e})"


def _refine_steps(
    screenshot_png: bytes,
    instruction: str,
    task_config: dict,
    current_steps: str,
    feedback: str,
) -> str:
    """Refine suggested steps based on user feedback.

    Sends the original screenshot, instruction, current steps, and the
    user's feedback back to the VLM for a revised set of steps.
    """
    import base64

    b64 = base64.b64encode(screenshot_png).decode()
    setup_desc = _build_setup_desc(task_config)

    prompt = f"""You are helping a human perform a task on a Windows desktop via VNC.

TASK: {instruction}

ENVIRONMENT SETUP (already done automatically):
{setup_desc}

You previously suggested these steps:

{current_steps}

The user, who can see the actual screen, provides this additional context:

"{feedback}"

First, re-examine the screenshot carefully. Describe what you actually see
(cell values, window titles, visible text, etc.) in 1-2 sentences.
Then produce REVISED step-by-step instructions that incorporate the user's
observations. The user may have noticed something you missed — give their
feedback careful consideration.
Keep the same format: plain text, numbered list, one action per step, concise."""

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{b64}",
                        "detail": "high",
                    },
                },
            ],
        }
    ]

    try:
        return _vlm_call(messages)
    except Exception as e:
        print(f"  (Refinement failed: {e})")
        return current_steps


def _parse_step_list(steps_text: str) -> list[str]:
    """Parse numbered VLM output into a list of step strings.

    Handles formats like "1. Do X", "1) Do X", "1: Do X".
    Falls back to non-empty lines if no numbered items are found.
    """
    import re

    lines = steps_text.strip().splitlines()
    steps: list[str] = []
    current_step: list[str] = []

    for line in lines:
        # Check if this line starts a new numbered step
        m = re.match(r"^\s*\d+[\.\)\:]\s+(.*)", line)
        if m:
            if current_step:
                steps.append(" ".join(current_step))
            current_step = [m.group(1).strip()]
        elif current_step:
            # Continuation line for the current step
            stripped = line.strip()
            if stripped:
                current_step.append(stripped)

    if current_step:
        steps.append(" ".join(current_step))

    # Fallback: if no numbered items found, split on non-empty lines
    if not steps:
        steps = [line.strip() for line in lines if line.strip()]

    return steps


def _format_step_list(steps: list[str], start_num: int = 1) -> str:
    """Format a list of step strings back into numbered text."""
    return "\n".join(
        f"{i}. {step}" for i, step in enumerate(steps, start=start_num)
    )


def _display_steps(steps_text: str) -> None:
    """Pretty-print suggested steps in a box."""
    print()
    print("  ┌─ SUGGESTED STEPS ──────────────────────────────")
    for line in steps_text.splitlines():
        print(f"  │ {line}")
    print("  └────────────────────────────────────────────────")
    print()


def _display_current_step(step_num: int, total: int, step_text: str) -> None:
    """Display the current step in a box before the action prompt."""
    header = f" Step {step_num} of {total} "
    width = max(len(header) + 4, len(step_text) + 6, 50)
    bar = "─" * (width - len(header) - 2)
    print(f"\n  ──{header}{bar}")
    print(f"  │ {step_text}")
    print(f"  └{'─' * (width)}")


def _interactive_step_review(
    screenshot_png: bytes,
    instruction: str,
    task_config: dict,
    initial_steps: str,
) -> str:
    """Let the user review and iteratively correct the suggested steps.

    The user can press Enter to accept, or type feedback to refine.
    Returns the final accepted steps text.
    """
    current = initial_steps
    while True:
        correction = input(
            "  Press Enter to accept steps, or type correction: "
        ).strip()
        if not correction:
            return current
        print("  Refining steps...")
        current = _refine_steps(
            screenshot_png, instruction, task_config, current, correction,
        )
        _display_steps(current)


def _refine_remaining_steps(
    screenshot_png: bytes,
    instruction: str,
    task_config: dict,
    completed_steps: list[str],
    remaining_steps: list[str],
    feedback: str,
) -> str:
    """Refine remaining steps based on user feedback mid-recording.

    Takes a FRESH screenshot (current screen state after completed steps)
    and sends it along with the context of what's been done and what remains.
    Returns the raw VLM text for remaining steps.
    """
    import base64

    b64 = base64.b64encode(screenshot_png).decode()
    setup_desc = _build_setup_desc(task_config)

    completed_text = (
        _format_step_list(completed_steps)
        if completed_steps
        else "(none)"
    )
    remaining_text = _format_step_list(remaining_steps)
    current_step_num = len(completed_steps) + 1

    prompt = f"""You are helping a human perform a task on a Windows desktop via VNC.

TASK: {instruction}

ENVIRONMENT SETUP (already done automatically):
{setup_desc}

The user has already completed these steps:
{completed_text}

You previously suggested these REMAINING steps (not yet performed):
{remaining_text}

The user is currently on step {current_step_num} and provides this context:
"{feedback}"

First, re-examine the screenshot carefully. Describe what you actually see
on the current screen in 1-2 sentences.
Then produce REVISED remaining steps that incorporate the user's observations.
The user can see the actual screen — give their feedback careful consideration.
Only output the remaining steps (do not repeat completed steps).
Keep the same format: plain text, numbered list starting from 1,
one action per step, concise."""

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{b64}",
                        "detail": "high",
                    },
                },
            ],
        }
    ]

    try:
        return _vlm_call(messages)
    except Exception as e:
        print(f"  (Refinement failed: {e})")
        return _format_step_list(remaining_steps)


def _interactive_remaining_review(
    server: str,
    instruction: str,
    task_config: dict,
    completed_steps: list[str],
    remaining_steps: list[str],
    initial_feedback: str,
) -> list[str]:
    """Mini review loop for mid-recording step refinement.

    Takes a fresh screenshot for each refinement round.
    Returns the accepted list of remaining steps.
    """
    # First refinement using the initial feedback
    fresh_png = _take_screenshot(server)
    refined_text = _refine_remaining_steps(
        fresh_png, instruction, task_config,
        completed_steps, remaining_steps, initial_feedback,
    )
    new_steps = _parse_step_list(refined_text)

    if not new_steps:
        print("  WARNING: VLM returned no steps. Keeping previous plan.")
        return remaining_steps

    start_num = len(completed_steps) + 1
    while True:
        # Display corrected remaining steps (numbered from where we left off)
        total = len(completed_steps) + len(new_steps)
        print()
        print(f"  ┌─ CORRECTED REMAINING STEPS ({start_num}-{total} of {total}) ──")
        for line in _format_step_list(new_steps, start_num=start_num).splitlines():
            print(f"  │ {line}")
        print("  └─────────────────────────────────────────────────")
        print()

        correction = input(
            "  Press Enter to accept, or type another correction: "
        ).strip()
        if not correction:
            return new_steps

        print("  Taking fresh screenshot and refining...")
        fresh_png = _take_screenshot(server)
        refined_text = _refine_remaining_steps(
            fresh_png, instruction, task_config,
            completed_steps, new_steps, correction,
        )
        parsed = _parse_step_list(refined_text)
        if parsed:
            new_steps = parsed
        else:
            print("  WARNING: VLM returned no steps. Keeping previous plan.")


# ---------------------------------------------------------------------------
# Auto-infrastructure helpers
# ---------------------------------------------------------------------------

_AUTO_VM_NAME = "waa-pool-00"
_AUTO_RESOURCE_GROUP = "openadapt-agents"
_AUTO_SSH_USER = "azureuser"

# Track whether this script started the VM (so we can offer to deallocate on exit)
_vm_started_by_script = False
_cleanup_registered = False
_cleanup_done = False


def _deallocate_vm() -> bool:
    """Deallocate the Azure VM (async, returns immediately). Returns True on success."""
    print(f"\n  Deallocating VM '{_AUTO_VM_NAME}'...")
    try:
        result = subprocess.run(
            [
                "az", "vm", "deallocate",
                "-g", _AUTO_RESOURCE_GROUP,
                "-n", _AUTO_VM_NAME,
                "--no-wait",
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            print(f"  VM '{_AUTO_VM_NAME}' deallocate initiated (billing will stop shortly).")
            return True
        else:
            print(f"  WARNING: deallocate failed: {result.stderr.strip()}")
            return False
    except Exception as e:
        print(f"  WARNING: deallocate failed: {e}")
        return False


def _cleanup_on_exit(signal_received: bool = False) -> None:
    """Offer to deallocate the VM if this script started it.

    Called from atexit (interactive prompt) or signal handler (auto-deallocate).
    """
    global _cleanup_done
    if _cleanup_done or not _vm_started_by_script:
        return
    _cleanup_done = True

    if signal_received:
        # Non-interactive (signal handler) — deallocate automatically
        print("\n\n  Script interrupted. Deallocating VM to stop billing...")
        _deallocate_vm()
    else:
        # Interactive (atexit / normal exit) — ask user
        try:
            answer = input(
                f"\n  This script started VM '{_AUTO_VM_NAME}'. "
                "Deallocate to stop billing? [Y/n] "
            ).strip().lower()
            if answer in ("", "y", "yes"):
                _deallocate_vm()
            else:
                print(f"  VM '{_AUTO_VM_NAME}' left running. "
                      f"Deallocate manually: az vm deallocate -g {_AUTO_RESOURCE_GROUP} "
                      f"-n {_AUTO_VM_NAME} --no-wait")
        except (EOFError, KeyboardInterrupt):
            # stdin closed or user hit ctrl+c during prompt — deallocate
            print()
            _deallocate_vm()


def _register_vm_cleanup() -> None:
    """Register atexit and signal handlers for VM cleanup. Idempotent."""
    global _cleanup_registered
    if _cleanup_registered:
        return
    _cleanup_registered = True

    import atexit
    import signal

    atexit.register(_cleanup_on_exit, signal_received=False)

    _original_sigint = signal.getsignal(signal.SIGINT)
    _original_sigterm = signal.getsignal(signal.SIGTERM)

    def _signal_handler(signum, frame):
        _cleanup_on_exit(signal_received=True)
        # Re-raise with original handler
        if signum == signal.SIGINT and callable(_original_sigint):
            _original_sigint(signum, frame)
        elif signum == signal.SIGTERM and callable(_original_sigterm):
            _original_sigterm(signum, frame)
        else:
            sys.exit(1)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

# Port mappings: local_port -> remote_port (on VM host)
_TUNNEL_PORTS = {
    5001: 5000,   # WAA server
    5050: 5051,   # evaluate server (via socat)
    8006: 8006,   # VNC (noVNC)
}


def _is_local_port_open(port: int) -> bool:
    """Check whether a local TCP port is accepting connections."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        return sock.connect_ex(("localhost", port)) == 0


def _get_vm_power_state() -> str | None:
    """Return the Azure VM power state, e.g. 'running', 'deallocated', or None on error."""
    try:
        result = subprocess.run(
            [
                "az", "vm", "get-instance-view",
                "-g", _AUTO_RESOURCE_GROUP,
                "-n", _AUTO_VM_NAME,
                "--query", "instanceView.statuses[?starts_with(code,'PowerState/')].displayStatus | [0]",
                "-o", "tsv",
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            # e.g. "VM running" -> "running", "VM deallocated" -> "deallocated"
            return result.stdout.strip().replace("VM ", "").lower()
    except Exception:
        pass
    return None


def _auto_start_vm() -> bool:
    """Start the Azure VM. Returns True on success."""
    global _vm_started_by_script
    print(f"  Starting VM '{_AUTO_VM_NAME}'...")
    result = subprocess.run(
        ["az", "vm", "start", "-g", _AUTO_RESOURCE_GROUP, "-n", _AUTO_VM_NAME],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        print(f"  ERROR: az vm start failed: {result.stderr.strip()}")
        return False
    print(f"  VM '{_AUTO_VM_NAME}' started.")
    _vm_started_by_script = True
    _register_vm_cleanup()
    return True


def _get_vm_public_ip() -> str | None:
    """Get the public IP of the VM."""
    try:
        result = subprocess.run(
            [
                "az", "vm", "show",
                "-g", _AUTO_RESOURCE_GROUP,
                "-n", _AUTO_VM_NAME,
                "--show-details",
                "--query", "publicIps",
                "-o", "tsv",
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _has_autossh() -> bool:
    """Check whether autossh is available on PATH."""
    import shutil
    return shutil.which("autossh") is not None


def _auto_establish_tunnels(vm_ip: str) -> bool:
    """Establish SSH tunnels to the VM. Returns True on success.

    Prefers ``autossh`` for automatic reconnection on tunnel drops.
    Falls back to plain ``ssh`` if autossh is not installed.
    """
    use_autossh = _has_autossh()
    tool = "autossh" if use_autossh else "ssh"
    print(f"  Establishing SSH tunnels to {vm_ip} (using {tool})...")

    # Build -L flags for all port mappings
    tunnel_args = []
    for local_port, remote_port in _TUNNEL_PORTS.items():
        tunnel_args.extend(["-L", f"{local_port}:localhost:{remote_port}"])

    ssh_opts = [
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ExitOnForwardFailure=yes",
        "-N",
    ]

    if use_autossh:
        # autossh monitoring port (0 = use ServerAlive instead of monitor port)
        cmd = ["autossh", "-M", "0", "-f"] + ssh_opts + tunnel_args + [
            f"{_AUTO_SSH_USER}@{vm_ip}"
        ]
    else:
        cmd = ["ssh", "-f"] + ssh_opts + tunnel_args + [
            f"{_AUTO_SSH_USER}@{vm_ip}"
        ]
        if not use_autossh:
            print("  (install autossh for automatic tunnel reconnection)")

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        print(f"  ERROR: {tool} tunnel failed: {result.stderr.strip()}")
        return False

    # Verify tunnels came up (give SSH a moment)
    time.sleep(2)
    all_ok = True
    for local_port in _TUNNEL_PORTS:
        if _is_local_port_open(local_port):
            print(f"    Tunnel localhost:{local_port} -> VM:{_TUNNEL_PORTS[local_port]}: OK")
        else:
            print(f"    Tunnel localhost:{local_port} -> VM:{_TUNNEL_PORTS[local_port]}: NOT YET")
            all_ok = False

    if not all_ok:
        print("  Some tunnels not yet accepting connections (WAA may still be booting).")

    return True


def _auto_start_container(vm_ip: str) -> bool:
    """Start the winarena Docker container on the VM. Returns True on success."""
    print(f"  Starting Docker container 'winarena' on {vm_ip}...")
    result = subprocess.run(
        ["ssh",
         "-o", "ConnectTimeout=10",
         "-o", "StrictHostKeyChecking=no",
         "-o", "UserKnownHostsFile=/dev/null",
         f"{_AUTO_SSH_USER}@{vm_ip}",
         "docker start winarena"],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        print(f"  ERROR: docker start failed: {result.stderr.strip()}")
        return False
    print("  Container started.")
    return True


def _auto_start_socat(vm_ip: str) -> bool:
    """Start socat proxy on the VM for port 5050 forwarding.

    Tries the socat-waa-evaluate systemd service first (preferred: auto-restarts
    on failure).  Falls back to the legacy nohup approach for older VMs that
    don't have the service installed.
    """
    print(f"  Starting socat proxy on {vm_ip} (VM:5051 -> container:5050)...")
    script = (
        "if systemctl list-unit-files socat-waa-evaluate.service "
        "| grep -q socat-waa-evaluate; then "
        "  sudo systemctl restart socat-waa-evaluate.service; "
        "else "
        "  killall socat 2>/dev/null || true; sleep 1; "
        "  which socat >/dev/null 2>&1 "
        "  || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq socat; "
        "  nohup socat TCP-LISTEN:5051,fork,reuseaddr "
        "  'EXEC:docker exec -i winarena socat - TCP\\:127.0.0.1\\:5050' "
        "  </dev/null >/dev/null 2>&1 & "
        "fi"
    )
    result = subprocess.run(
        ["ssh",
         "-o", "ConnectTimeout=10",
         "-o", "StrictHostKeyChecking=no",
         "-o", "UserKnownHostsFile=/dev/null",
         f"{_AUTO_SSH_USER}@{vm_ip}",
         script],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        print(f"  WARNING: socat setup returned non-zero: {result.stderr.strip()}")
        # Not fatal — socat may already be running
    else:
        print("  Socat proxy established (VM:5051 -> container:5050).")
    return True


def _wait_for_waa_ready(server: str, timeout: int = 600, interval: int = 5) -> bool:
    """Poll the WAA /probe endpoint until it returns HTTP 200.

    Args:
        server: WAA server URL (e.g. http://localhost:5001).
        timeout: Maximum seconds to wait.
        interval: Seconds between polls.

    Returns:
        True if WAA became ready within the timeout.
    """
    import requests

    print(f"  Waiting for WAA to become ready at {server}/probe (timeout {timeout}s)...")
    start = time.monotonic()
    attempts = 0
    while time.monotonic() - start < timeout:
        attempts += 1
        elapsed = int(time.monotonic() - start)
        try:
            resp = requests.get(f"{server}/probe", timeout=5)
            if resp.status_code == 200:
                print(f"  WAA is ready (after {elapsed}s, {attempts} attempts).")
                return True
            print(f"    [{elapsed}s] probe returned {resp.status_code}, retrying...")
        except requests.ConnectionError:
            print(f"    [{elapsed}s] connection refused, retrying...")
        except Exception as e:
            print(f"    [{elapsed}s] {e}, retrying...")
        time.sleep(interval)

    print(f"  TIMEOUT: WAA did not become ready within {timeout}s.")
    return False


def _attempt_auto_recovery(
    server: str,
    auto_vm: bool,
    auto_tunnel: bool,
    auto_container: bool,
    wait_ready: bool,
) -> bool:
    """Attempt to automatically bring up WAA infrastructure.

    Runs the appropriate recovery steps based on flags. Returns True if
    WAA is reachable after recovery.
    """
    print()
    print("  Auto-recovery: diagnosing infrastructure state...")

    # Step 1: Check/start VM
    vm_ip = _get_vm_public_ip()
    power_state = _get_vm_power_state()
    print(f"  VM power state: {power_state or 'unknown'}")

    if power_state != "running":
        if auto_vm:
            if not _auto_start_vm():
                print("  FAILED: Could not start VM.")
                return False
            # Re-fetch IP after start (it may change on deallocated VMs)
            time.sleep(5)
            vm_ip = _get_vm_public_ip()
            if not vm_ip:
                print("  FAILED: VM started but could not get public IP.")
                return False
            print(f"  VM IP: {vm_ip}")
        else:
            print("  VM is not running. Use --auto-vm to start it automatically.")
            return False
    else:
        if not vm_ip:
            vm_ip = _get_vm_public_ip()
        print(f"  VM IP: {vm_ip}")

    if not vm_ip:
        print("  FAILED: Could not determine VM IP.")
        return False

    # Step 2: Check/start Docker container
    if auto_container:
        _auto_start_container(vm_ip)
        _auto_start_socat(vm_ip)

    # Step 3: Check/establish SSH tunnels
    tunnels_needed = not _is_local_port_open(5001)
    if tunnels_needed:
        if auto_tunnel:
            if not _auto_establish_tunnels(vm_ip):
                print("  FAILED: Could not establish SSH tunnels.")
                return False
        else:
            print("  SSH tunnels are not active. Use --auto-tunnel to establish them.")
            return False
    else:
        print("  SSH tunnels: already active (localhost:5001 is open)")

    # Step 4: Wait for WAA to be ready
    if wait_ready:
        return _wait_for_waa_ready(server)

    # If not waiting, just check once
    import requests
    try:
        resp = requests.get(f"{server}/probe", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


# Default external backup root (outside the git repo)
_BACKUP_ROOT = Path.home() / "oa" / "recordings"


def _backup_file(src: Path, task_id: str) -> None:
    """Hardlink *src* to the external backup directory.

    Creates ``~/oa/recordings/{task_id}/{src.name}``.  Uses a hardlink (zero
    extra disk space) and falls back to ``shutil.copy2`` for cross-device
    scenarios.  Failures are silently ignored — recording must never be
    interrupted by a backup error.
    """
    try:
        backup_dir = _BACKUP_ROOT / task_id
        backup_dir.mkdir(parents=True, exist_ok=True)
        dest = backup_dir / src.name
        if dest.exists():
            dest.unlink()
        try:
            os.link(src, dest)
        except OSError:
            shutil.copy2(src, dest)
    except Exception:
        pass  # Silent — never interrupt recording


def _save_incremental_meta(
    task_dir: Path,
    task_id: str,
    instruction: str,
    steps_meta: list[dict],
    step_plans: list[dict],
    server: str,
    is_final: bool = False,
) -> None:
    """Write ``meta.json`` atomically after each step.

    Writes to a ``.tmp`` file first and renames, preventing corrupt partial
    writes on crash.  Includes a ``recording_complete`` boolean so downstream
    scripts can detect partial recordings.
    """
    meta = {
        "task_id": task_id,
        "instruction": instruction,
        "num_steps": len(steps_meta),
        "steps": steps_meta,
        "step_plans": step_plans,
        "server_url": server,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "recording_complete": is_final,
    }
    tmp = task_dir / "meta.json.tmp"
    final = task_dir / "meta.json"
    tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    tmp.replace(final)
    _backup_file(final, task_id)


def cmd_record_waa(
    tasks: str = ",".join(HARDER_TASK_IDS),
    server: str = "http://localhost:5001",
    evaluate_url: str = "http://localhost:5050",
    output: str = "waa_recordings",
    vnc_url: str = "http://localhost:8006",
    vm_ip: str | None = None,
    verify: bool = True,
    auto: bool = False,
    auto_vm: bool = False,
    auto_tunnel: bool = False,
    auto_container: bool = False,
    wait_ready: bool = True,
) -> None:
    """Record demos interactively via WAA API while user performs actions on VNC.

    Args:
        tasks: Comma-separated task IDs (or prefix matches).
        server: WAA server URL.
        evaluate_url: Evaluate server URL (for /task/<id> lookups).
        output: Output directory for recordings.
        vnc_url: VNC URL for the user to open in a browser.
        vm_ip: Azure VM IP for QEMU reset on task restart.
            Auto-detected from pool registry or Azure if omitted.
        verify: Pre-flight check that all required apps are installed (default True).
        auto: Automatically start all infrastructure (VM, tunnels, container).
            Equivalent to setting auto_vm, auto_tunnel, and auto_container.
        auto_vm: Start Azure VM if it is deallocated (incurs charges).
        auto_tunnel: Establish SSH tunnels if not connected.
        auto_container: Start Docker container and socat proxy if not running.
        wait_ready: Wait for WAA server to boot after recovery (default True).
    """
    # Guard: Fire may pass True if --tasks is used without a value
    if not isinstance(tasks, str):
        print("ERROR: --tasks must be a string of comma-separated task IDs.")
        print(f"  Got: {tasks!r} (type {type(tasks).__name__})")
        print("  Hint: use --tasks=\"id1,id2,...\" (with = and no space)")
        return

    # --auto is a convenience flag that enables all sub-flags
    if auto:
        auto_vm = True
        auto_tunnel = True
        auto_container = True

    any_auto = auto_vm or auto_tunnel or auto_container

    from openadapt_evals.infrastructure.vm_ip import resolve_vm_ip

    # VM IP resolution may fail if VM is deallocated — that's OK if we have
    # auto-recovery flags, since we'll start the VM first.
    try:
        vm_ip = resolve_vm_ip(vm_ip)
    except RuntimeError:
        if any_auto:
            vm_ip = None  # Will be resolved after VM start
        else:
            raise

    import requests

    output_dir = Path(output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Parse task IDs — support prefix matching against HARDER_TASK_IDS
    raw_ids = [t.strip() for t in tasks.split(",") if t.strip()]
    task_ids = []
    for raw in raw_ids:
        # Try exact match first
        if any(raw == tid for tid in HARDER_TASK_IDS):
            task_ids.append(raw)
        else:
            # Prefix match
            matches = [tid for tid in HARDER_TASK_IDS if tid.startswith(raw)]
            if len(matches) == 1:
                task_ids.append(matches[0])
            elif len(matches) > 1:
                print(f"Ambiguous prefix '{raw}' matches: {matches}")
                return
            else:
                # Not in HARDER_TASK_IDS — use as-is (custom task ID)
                task_ids.append(raw)

    # Verify connection
    print(f"Connecting to WAA server at {server}...")
    connected = False
    try:
        resp = requests.get(f"{server}/probe", timeout=5)
        resp.raise_for_status()
        print(f"  Connected ({resp.status_code})")
        connected = True
    except Exception as e:
        print(f"  Failed to connect: {e}")

        if any_auto:
            # Confirm with user if VM start is involved (cost warning)
            if auto_vm:
                power_state = _get_vm_power_state()
                if power_state != "running":
                    print()
                    print(
                        "  WARNING: --auto will start Azure VM resources "
                        "which incur charges."
                    )
                    answer = input("  Continue? [y/N] ").strip().lower()
                    if answer not in ("y", "yes"):
                        print("  Aborted.")
                        return

            recovered = _attempt_auto_recovery(
                server=server,
                auto_vm=auto_vm,
                auto_tunnel=auto_tunnel,
                auto_container=auto_container,
                wait_ready=wait_ready,
            )
            if recovered:
                print()
                print("  Auto-recovery succeeded. WAA is ready.")
                connected = True
                # Re-resolve VM IP now that infrastructure is up
                if vm_ip is None:
                    try:
                        vm_ip = resolve_vm_ip(vm_ip)
                    except RuntimeError:
                        vm_ip = _get_vm_public_ip()
            else:
                print()
                print("  Auto-recovery FAILED. Cannot proceed.")
                return
        else:
            print()
            print("  Make sure the WAA server is running and SSH tunnels are up.")
            print()
            print("  To auto-recover, re-run with --auto:")
            print(f"    python {__file__} record-waa --auto --tasks={tasks}")
            print()
            print("  Or use granular flags:")
            print("    --auto-vm        Start Azure VM (incurs charges)")
            print("    --auto-tunnel    Establish SSH tunnels")
            print("    --auto-container Start Docker container + socat proxy")
            return

    if not connected:
        return

    # Pre-fetch all task configs BEFORE QEMU reset.  The evaluate server
    # (localhost:5050) goes through a socat bridge that can become stale
    # after container/VM restarts.  Fetching early ensures we have the
    # human-readable instructions cached even if the bridge dies later.
    print("Pre-fetching task configs from evaluate server...")
    task_configs_cache: dict[str, dict] = {}
    fetch_failures = 0
    for task_id in task_ids:
        try:
            resp = requests.get(f"{evaluate_url}/task/{task_id}", timeout=10)
            if resp.ok:
                task_configs_cache[task_id] = resp.json()
        except Exception:
            fetch_failures += 1
    if task_configs_cache:
        print(f"  Cached {len(task_configs_cache)}/{len(task_ids)} task config(s).")
    if fetch_failures:
        print(f"  WARNING: {fetch_failures} task config(s) failed to fetch from {evaluate_url}.")
        print("  Step generation will use task IDs instead of instructions for those tasks.")
        print("  (Is the evaluate server / socat proxy running?)")
    if not task_configs_cache and len(task_ids) > 0:
        print(f"  ERROR: Could not fetch ANY task configs from {evaluate_url}.")
        print("  The evaluate server may be down. Check socat proxy on the VM.")
        answer = input("  Continue anyway? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            return
    print()

    # Pre-flight: verify all required apps are installed
    if verify:
        print("Verifying required apps across all tasks...")
        all_apps: set[str] = set()
        for tc in task_configs_cache.values():
            all_apps.update(tc.get("related_apps", []))
        if all_apps:
            resp = requests.post(
                f"{evaluate_url}/setup",
                json={"config": [{"type": "verify_apps", "parameters": {"apps": list(all_apps)}}]},
                timeout=30,
            )
            if resp.status_code != 200:
                try:
                    errors = [
                        r for r in resp.json().get("results", [])
                        if r.get("status") == "error"
                    ]
                except (ValueError, KeyError):
                    errors = [{"error": f"Server returned {resp.status_code}: {resp.text[:200]}"}]
                for err in errors:
                    print(f"  ERROR: {err.get('error', '?')}")
                answer = input("  Missing apps detected. Continue anyway? [y/N] ").strip().lower()
                if answer not in ("y", "yes"):
                    print("  Aborting. Install missing apps and retry.")
                    return
            else:
                print(f"  All {len(all_apps)} required app(s) present.")
        else:
            print("  No app checks needed (no related_apps in task configs).")
        print()

    print(f"Recording {len(task_ids)} task(s) to {output_dir}")
    print(f"\n  VNC (open in browser): {vnc_url}\n")

    from openadapt_evals.infrastructure.qemu_reset import QEMUResetManager

    # Check if any task has a resumable checkpoint BEFORE resetting
    resumable_task = None
    for _tid in task_ids:
        _tdir = output_dir / _tid
        _ckpt = _load_checkpoint(_tdir)
        if _ckpt is not None:
            n_done = len(_ckpt["completed_steps"])
            n_left = len(_ckpt["remaining_steps"])
            ts = _ckpt.get("timestamp", "unknown")
            next_step = _ckpt["remaining_steps"][0] if _ckpt["remaining_steps"] else "(done)"
            print(f"  Checkpoint found for {_tid[:12]}...")
            print(f"    {n_done} step(s) done, {n_left} remaining (saved {ts})")
            print(f"    Next step: {next_step}")
            print(f"\n  Check VNC ({vnc_url}) — is the VM still where you left off?")
            print("  If yes, we can skip the hard reset and resume recording.")
            answer = input("  Resume from checkpoint? [y/N] ").strip().lower()
            if answer in ("y", "yes"):
                resumable_task = _tid
            break  # Only offer the first checkpoint found

    if resumable_task is None:
        # No resume — hard reset Windows for clean slate
        print("\n  WARNING: This tool uses QEMU hard resets between tasks.")
        print("  LibreOffice document recovery is cleared automatically")
        print("  after each reset. Any unsaved work in the VM will be lost.\n")

        print("Resetting Windows to clean state before recording...")
        mgr = QEMUResetManager(vm_ip=vm_ip, timeout_seconds=300)
        success, msg = mgr.restart_windows(server_url=server)
        if success:
            print(f"  {msg}")
        else:
            print(f"  WARNING: QEMU reset failed: {msg}")
            print("  Continuing anyway — desktop may have stale state.")

        _clear_recovery_data(server)
    else:
        print("  Skipping hard reset (resuming from checkpoint).")
    print()

    recorded = []
    for task_num, task_id in enumerate(task_ids, 1):
        print_header(f"Task {task_num}/{len(task_ids)}: {task_id[:12]}...")

        task_dir = output_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        # Load task config — prefer pre-fetched cache, fall back to live fetch
        task_config = task_configs_cache.get(task_id, {})
        if not task_config:
            # Try live fetch (cache miss or evaluate server was down earlier)
            for _attempt in range(3):
                try:
                    task_resp = requests.get(
                        f"{evaluate_url}/task/{task_id}", timeout=10
                    )
                    if task_resp.ok:
                        task_config = task_resp.json()
                        break
                except Exception as e:
                    if _attempt < 2:
                        print(f"  Warning: task config fetch failed ({e}), retrying...")
                        time.sleep(2)
                    else:
                        print(f"  Warning: could not load task config after 3 attempts: {e}")
        instruction = task_config.get(
            "instruction", task_config.get("task", task_id)
        )

        def _setup_task_env() -> None:
            """Run task setup config (download files, open apps, etc.)."""
            setup_config = task_config.get("config", [])
            related_apps = task_config.get("related_apps", [])
            if related_apps:
                setup_config = [
                    {"type": "verify_apps", "parameters": {"apps": related_apps}}
                ] + setup_config
            if setup_config:
                resp = requests.post(
                    f"{evaluate_url}/setup",
                    json={"config": setup_config},
                    timeout=120,
                )
                resp.raise_for_status()
                results = resp.json().get("results", [])
                for r in results:
                    status = r.get("status", "?")
                    stype = r.get("type", "?")
                    print(f"    setup {stype}: {status}")

        def _soft_reset_task_env() -> bytes:
            """Soft reset: close_all + re-run setup + wait for stable screen."""
            print("  Resetting environment (soft)...")
            try:
                resp = requests.post(f"{server}/setup/close_all", timeout=30)
                print(f"    close_all: {resp.status_code}")
                time.sleep(3)
                _setup_task_env()
                time.sleep(5)  # Give the app time to open after setup
            except Exception as e:
                print(f"  WARNING: environment setup failed: {e}")
                print("  The task app may not be open. Check VNC.")
            print("  Waiting for screen to stabilize...")
            return _wait_for_stable_screen(server)

        def _hard_reset_task_env() -> bytes:
            """Hard reset: QEMU system_reset + wait for boot + clear recovery + setup + stable screen."""
            print("  Restarting Windows (QEMU hard reset)...")
            mgr = QEMUResetManager(vm_ip=vm_ip, timeout_seconds=300)
            success, msg = mgr.restart_windows(server_url=server)
            if not success:
                print(f"  WARNING: QEMU reset failed: {msg}")
                print("  Falling back to soft reset...")
                return _soft_reset_task_env()
            print(f"    {msg}")
            _clear_recovery_data(server)
            print("  Running task setup...")
            try:
                _setup_task_env()
            except Exception as e:
                print(f"  WARNING: environment setup failed: {e}")
                print("  The task app may not be open. Check VNC.")
            print("  Waiting for screen to stabilize...")
            return _wait_for_stable_screen(server)

        # Resume decision was already made at startup (before hard reset).
        # If the user chose to resume, we skipped the reset and the VM state
        # should be intact.  Otherwise, the VM was rebooted and any checkpoint
        # is stale.
        resuming = (resumable_task == task_id)
        checkpoint = _load_checkpoint(task_dir) if resuming else None
        if not resuming:
            _delete_checkpoint(task_dir)

        if resuming:
            # Restore state from checkpoint
            completed_steps = checkpoint["completed_steps"]
            remaining_steps = checkpoint["remaining_steps"]
            step_plans = checkpoint["step_plans"]
            refined_indices = set(checkpoint["refined_indices"])
            steps_meta = checkpoint["steps_meta"]
            step_idx = checkpoint["step_idx"]
            # Use the instruction from checkpoint if task config fetch failed
            if instruction == task_id:
                instruction = checkpoint.get("instruction", task_id)

            print(f"\n  Resumed at step {step_idx + 1} "
                  f"({len(completed_steps)} done, "
                  f"{len(remaining_steps)} remaining)")
            print(f"  VNC: {vnc_url}")
            print(f"  Task: {instruction}\n")

            # Take a fresh screenshot as the "before" for the next step
            before_png = _take_screenshot(server)
        else:
            # Fresh start — delete any stale checkpoint
            _delete_checkpoint(task_dir)

            before_png = _soft_reset_task_env()

            print(f"\n  VNC: {vnc_url}")
            print(f"  Task: {instruction}\n")

            # Generate AI step-by-step guidance from screenshot
            print("  Generating suggested steps...")
            suggested = _generate_steps(before_png, instruction, task_config)
            _display_steps(suggested)
            suggested = _interactive_step_review(
                before_png, instruction, task_config, suggested,
            )

            # Parse accepted steps into a structured list
            completed_steps: list[str] = []
            remaining_steps = _parse_step_list(suggested)
            step_plans = [{
                "at_step_idx": 0,
                "trigger": "initial",
                "steps": list(remaining_steps),
            }]
            refined_indices: set[int] = set()
            steps_meta: list[dict] = []
            step_idx = 0

        print()
        print("  Perform each action in VNC. You can provide feedback")
        print("  at any point to correct the remaining steps.\n")

        while remaining_steps:
            # Save before screenshot
            before_path = task_dir / f"step_{step_idx:02d}_before.png"
            before_path.write_bytes(before_png)
            _backup_file(before_path, task_id)

            # Display current step
            total = len(completed_steps) + len(remaining_steps)
            step_num = len(completed_steps) + 1
            _display_current_step(step_num, total, remaining_steps[0])

            user_input = input(
                "  [Enter] next step   [x] retry step   [u] undo prev step\n"
                "  [d] task complete   [s] refresh steps from screenshot\n"
                "  [r] restart task    [R] restart task (reboot VM)\n"
                "  Or type correction: "
            ).strip()

            if user_input.lower() == "x":
                # RETRY: discard this attempt, take fresh before screenshot
                print("  Retrying step (taking fresh screenshot)...")
                before_png = _take_screenshot(server)
                retry_path = task_dir / f"step_{step_idx:02d}_before.png"
                retry_path.write_bytes(before_png)
                _backup_file(retry_path, task_id)
                continue

            elif user_input == "":
                # ADVANCE: action done, move to next step
                after_png = _take_screenshot(server)
                after_path = task_dir / f"step_{step_idx:02d}_after.png"
                after_path.write_bytes(after_png)
                _backup_file(after_path, task_id)
                done_step = remaining_steps.pop(0)
                completed_steps.append(done_step)
                steps_meta.append({
                    "action_hint": None,
                    "suggested_step": done_step,
                    "step_was_refined": step_idx in refined_indices,
                })
                _save_incremental_meta(
                    task_dir, task_id, instruction, steps_meta,
                    step_plans, server,
                )
                before_png = after_png
                step_idx += 1
                print(f"  Step {step_num} recorded.")

                # Checkpoint after each completed step
                _save_checkpoint(
                    task_dir, task_id, instruction,
                    completed_steps, remaining_steps,
                    step_plans, refined_indices,
                    steps_meta, step_idx,
                )

                if not remaining_steps:
                    print(f"\n  All {total} steps completed. Finishing recording.")

            elif user_input.lower() == "d":
                # DONE: task finished (possibly before all steps)
                after_png = _take_screenshot(server)
                after_path = task_dir / f"step_{step_idx:02d}_after.png"
                after_path.write_bytes(after_png)
                _backup_file(after_path, task_id)
                steps_meta.append({
                    "action_hint": "d",
                    "suggested_step": remaining_steps[0],
                    "step_was_refined": step_idx in refined_indices,
                })
                _save_incremental_meta(
                    task_dir, task_id, instruction, steps_meta,
                    step_plans, server,
                )
                step_idx += 1
                total = len(completed_steps) + len(remaining_steps)
                print(f"\n  Task marked done at step {step_num} of {total}. Finishing recording.")

                # Checkpoint the final state before writing meta.json
                _save_checkpoint(
                    task_dir, task_id, instruction,
                    completed_steps, remaining_steps,
                    step_plans, refined_indices,
                    steps_meta, step_idx,
                )
                break

            elif user_input.lower() == "u":
                # UNDO: go back one step
                if not completed_steps:
                    print("  Nothing to undo (already at step 1).")
                    continue
                step_idx -= 1
                remaining_steps.insert(0, completed_steps.pop())
                steps_meta.pop()
                before_png = _take_screenshot(server)
                print(f"  Undid last step. Now at step {len(completed_steps) + 1}.")

                # Checkpoint after undo
                _save_checkpoint(
                    task_dir, task_id, instruction,
                    completed_steps, remaining_steps,
                    step_plans, refined_indices,
                    steps_meta, step_idx,
                )

            elif user_input == "r":
                # RESTART (soft): close all apps, re-run setup, regenerate steps
                print("  Restarting task (soft reset — closing apps, re-running setup)...")
                for f in task_dir.glob("step_*.png"):
                    f.unlink()
                # Clean external backup for this task too
                backup_dir = _BACKUP_ROOT / task_id
                if backup_dir.exists():
                    shutil.rmtree(backup_dir, ignore_errors=True)
                before_png = _soft_reset_task_env()
                print(f"\n  VNC: {vnc_url}")
                print(f"  Task: {instruction}\n")

                print("  Generating suggested steps...")
                new_suggested = _generate_steps(before_png, instruction, task_config)
                _display_steps(new_suggested)
                new_suggested = _interactive_step_review(
                    before_png, instruction, task_config, new_suggested,
                )

                completed_steps = []
                remaining_steps = _parse_step_list(new_suggested)
                step_plans.append({
                    "at_step_idx": 0,
                    "trigger": "soft_restart",
                    "steps": list(remaining_steps),
                })
                refined_indices = set()
                steps_meta = []
                step_idx = 0

                _save_checkpoint(
                    task_dir, task_id, instruction,
                    completed_steps, remaining_steps,
                    step_plans, refined_indices,
                    steps_meta, step_idx,
                )
                print()
                print("  Task restarted (soft). Continue recording.\n")

            elif user_input == "R":
                # RESTART (hard): QEMU hard reset + re-generate everything
                print("  Restarting task (hard reset — QEMU reboot)...")
                for f in task_dir.glob("step_*.png"):
                    f.unlink()
                # Clean external backup for this task too
                backup_dir = _BACKUP_ROOT / task_id
                if backup_dir.exists():
                    shutil.rmtree(backup_dir, ignore_errors=True)
                before_png = _hard_reset_task_env()
                print(f"\n  VNC: {vnc_url}")
                print(f"  Task: {instruction}\n")

                print("  Generating suggested steps...")
                new_suggested = _generate_steps(before_png, instruction, task_config)
                _display_steps(new_suggested)
                new_suggested = _interactive_step_review(
                    before_png, instruction, task_config, new_suggested,
                )

                completed_steps = []
                remaining_steps = _parse_step_list(new_suggested)
                step_plans.append({
                    "at_step_idx": 0,
                    "trigger": "restart",
                    "steps": list(remaining_steps),
                })
                refined_indices = set()
                steps_meta = []
                step_idx = 0

                # Checkpoint after restart
                _save_checkpoint(
                    task_dir, task_id, instruction,
                    completed_steps, remaining_steps,
                    step_plans, refined_indices,
                    steps_meta, step_idx,
                )
                print()
                print("  Task restarted. Continue recording.\n")

            elif user_input.lower() == "s":
                # REFRESH: regenerate remaining steps from current screenshot
                print("  Taking fresh screenshot and regenerating remaining steps...")
                before_png = _take_screenshot(server)
                new_suggested = _generate_steps(before_png, instruction, task_config)
                new_steps = _parse_step_list(new_suggested)
                if new_steps:
                    _display_steps(new_suggested)
                    new_suggested = _interactive_step_review(
                        before_png, instruction, task_config, new_suggested,
                    )
                    remaining_steps = _parse_step_list(new_suggested)
                    step_plans.append({
                        "at_step_idx": step_idx,
                        "trigger": "screenshot_refresh",
                        "steps": list(remaining_steps),
                    })
                    for i in range(step_idx, step_idx + len(remaining_steps)):
                        refined_indices.add(i)

                    _save_checkpoint(
                        task_dir, task_id, instruction,
                        completed_steps, remaining_steps,
                        step_plans, refined_indices,
                        steps_meta, step_idx,
                    )
                else:
                    print("  WARNING: VLM returned no steps. Keeping previous plan.")

            else:
                # FEEDBACK: mid-recording step correction
                print("  Taking fresh screenshot and refining remaining steps...")
                remaining_steps = _interactive_remaining_review(
                    server, instruction, task_config,
                    completed_steps, remaining_steps, user_input,
                )
                step_plans.append({
                    "at_step_idx": step_idx,
                    "trigger": f"feedback: {user_input}",
                    "steps": list(remaining_steps),
                })
                for i in range(step_idx, step_idx + len(remaining_steps)):
                    refined_indices.add(i)

                # Checkpoint after step refinement
                _save_checkpoint(
                    task_dir, task_id, instruction,
                    completed_steps, remaining_steps,
                    step_plans, refined_indices,
                    steps_meta, step_idx,
                )
                # No action taken — loop re-displays the (possibly new) current step

        # Save final metadata (marks recording as complete)
        _save_incremental_meta(
            task_dir, task_id, instruction, steps_meta,
            step_plans, server, is_final=True,
        )

        # Task completed successfully — remove checkpoint
        _delete_checkpoint(task_dir)

        recorded.append(task_id)
        print(f"\n  Saved {len(steps_meta)} step(s) to {task_dir}")
        track_demo_recorded(
            task_id=task_id,
            mode="record-waa",
            steps=len(steps_meta),
            output_dir=str(output_dir),
            phase="recorded",
        )

    # Summary
    print_header("Recording Summary")
    print(f"  Recorded: {len(recorded)}/{len(task_ids)} tasks")
    for tid in recorded:
        print(f"    {tid}")
    print(f"\n  Output directory: {output_dir}")
    print(
        f"\n  Next: python {__file__} annotate "
        f"--recordings {output_dir} --output annotated_demos/"
    )


# ---------------------------------------------------------------------------
# VLM annotation of WAA recordings
# ---------------------------------------------------------------------------


def cmd_annotate_waa(
    recordings: str = "waa_recordings",
    output: str = "annotated_demos",
    provider: str = "openai",
    model: str | None = None,
    api_key: str | None = None,
) -> None:
    """Annotate WAA recordings with VLM to produce structured demo traces.

    Args:
        recordings: Directory containing task recording subdirectories.
        output: Output directory for annotated demo JSON/TXT files.
        provider: VLM provider ("openai", "anthropic", "google").
        model: Model override (default: provider's default).
        api_key: API key override.
    """
    from openadapt_evals.annotation import (
        ANNOTATION_STEP_PROMPT,
        ANNOTATION_SYSTEM_PROMPT,
        AnnotatedDemo,
        AnnotatedStep,
        format_annotated_demo,
        parse_annotation_response,
        validate_annotations,
    )
    from openadapt_evals.vlm import image_bytes_from_path, vlm_call

    recordings_dir = Path(recordings)
    output_dir = Path(output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Find task directories with meta.json
    task_dirs = sorted(
        d for d in recordings_dir.iterdir()
        if d.is_dir() and (d / "meta.json").exists()
    )

    if not task_dirs:
        print(f"No recordings found in {recordings_dir}")
        return

    print(f"Found {len(task_dirs)} recording(s) to annotate")
    print(f"Provider: {provider}, Model: {model or 'default'}\n")

    resolved_model = model or (
        "gpt-4.1-mini" if provider == "openai" else "claude-sonnet-4-20250514"
    )

    for task_dir in task_dirs:
        meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
        task_id = meta["task_id"]
        instruction = meta["instruction"]
        num_steps = meta["num_steps"]

        print(f"{'=' * 60}")
        print(f"Annotating: {task_id[:40]}... ({num_steps} steps)")
        print(f"{'=' * 60}")

        annotated_steps = []
        prev_annotation = None

        for i in range(num_steps):
            before_path = task_dir / f"step_{i:02d}_before.png"
            after_path = task_dir / f"step_{i:02d}_after.png"

            if not before_path.exists():
                print(f"  Step {i}: missing before screenshot, skipping")
                continue

            # Build action_raw from hints if available
            step_meta = meta.get("steps", [{}])
            action_hint = ""
            if i < len(step_meta):
                action_hint = step_meta[i].get("action_hint") or ""
            action_raw = action_hint or f"(user action at step {i + 1})"

            # Build previous context
            previous_context = ""
            if prev_annotation is not None:
                previous_context = (
                    f"Previous step: {prev_annotation.action}\n"
                    f"Previous result: {prev_annotation.result_observation}\n"
                )

            no_after_note = ""
            if not after_path.exists():
                no_after_note = (
                    " (No AFTER image available — describe expected result only.)"
                )

            prompt_text = ANNOTATION_STEP_PROMPT.format(
                instruction=instruction,
                step_num=i + 1,
                total_steps=num_steps,
                action_raw=action_raw,
                previous_context=previous_context,
                no_after_note=no_after_note,
            )

            # Build image list
            images = [image_bytes_from_path(before_path)]
            if after_path.exists():
                images.append(image_bytes_from_path(after_path))

            print(f"  Step {i + 1}/{num_steps}...", end=" ", flush=True)

            try:
                response = vlm_call(
                    prompt_text,
                    images=images,
                    system=ANNOTATION_SYSTEM_PROMPT,
                    model=resolved_model,
                    max_tokens=512,
                    temperature=0.1,
                    provider=provider,
                )
                parsed = parse_annotation_response(response)
            except Exception as e:
                print(f"error: {e}")
                parsed = {
                    "observation": "",
                    "intent": "",
                    "action": action_raw,
                    "result_observation": "",
                    "expected_result": "",
                }

            annotated = AnnotatedStep(
                step_index=i,
                timestamp_ms=None,
                observation=parsed.get("observation", ""),
                intent=parsed.get("intent", ""),
                action=parsed.get("action", action_raw),
                action_raw=action_raw,
                action_px=None,
                result_observation=parsed.get("result_observation", ""),
                expected_result=parsed.get("expected_result", ""),
            )
            annotated_steps.append(annotated)
            prev_annotation = annotated
            print("done")

        demo = AnnotatedDemo(
            schema_version="0.1",
            task_id=task_id,
            instruction=instruction,
            source="recorded",
            annotator={"provider": provider, "model": resolved_model},
            recording_meta={
                "platform": "windows",
                "screen_px": None,
                "source": "waa_api_recording",
                "raw_step_count": num_steps,
                "annotated_step_count": len(annotated_steps),
            },
            steps=annotated_steps,
        )

        # Save JSON
        json_path = output_dir / f"{task_id}.json"
        demo.save(json_path)

        # Save formatted text for eval-suite
        txt = format_annotated_demo(demo, compact=True)
        txt_path = output_dir / f"{task_id}.txt"
        txt_path.write_text(txt, encoding="utf-8")

        # Validate
        warnings = validate_annotations(demo)
        if warnings:
            print(f"  Warnings ({len(warnings)}):")
            for w in warnings:
                print(f"    - {w}")
        else:
            print("  Validation: all checks passed")

        print(f"  -> {json_path}")
        print(f"  -> {txt_path}\n")
        track_demo_recorded(
            task_id=task_id,
            mode="annotate",
            steps=len(annotated_steps),
            output_dir=str(output_dir),
            phase="annotated",
        )

    print_header("Annotation Summary")
    print(f"  Annotated: {len(task_dirs)} recording(s)")
    print(f"  Output: {output_dir}")
    print(
        f"\n  Next: python {__file__} eval "
        f"--demo_dir {output_dir} --tasks <task_ids>"
    )


# ---------------------------------------------------------------------------
# Demo-conditioned eval (delegates to eval-suite)
# ---------------------------------------------------------------------------


def cmd_eval_dc(
    demo_dir: str = "annotated_demos",
    tasks: str = ",".join(HARDER_TASK_IDS),
    agent: str = "api-openai",
    server: str = "http://localhost:5001",
    evaluate_url: str = "http://localhost:5050",
    max_steps: int = 15,
    output: str = "benchmark_results",
    suite_name: str | None = None,
) -> None:
    """Run demo-conditioned evaluation using eval-suite.

    Args:
        demo_dir: Directory with annotated demos (.json/.txt files).
        tasks: Comma-separated task IDs.
        agent: Agent type (api-openai, api-claude, api-claude-cu, qwen3vl).
        server: WAA server URL.
        evaluate_url: Evaluate server URL.
        max_steps: Maximum steps per task.
        output: Output directory for benchmark results.
        suite_name: Suite name prefix (default: auto-generated timestamp).
    """
    cmd = [
        sys.executable, "-m", "openadapt_evals.benchmarks.cli",
        "eval-suite",
        "--tasks", tasks,
        "--demo-dir", str(Path(demo_dir).resolve()),
        "--agent", agent,
        "--server", server,
        "--evaluate-url", evaluate_url,
        "--max-steps", str(max_steps),
        "--output", output,
        "--no-pool-create",
        "--no-pool-cleanup",
    ]
    if suite_name:
        cmd.extend(["--suite-name", suite_name])

    print(f"Running eval-suite with demo-conditioned demos from {demo_dir}")
    print(f"Command: {' '.join(cmd)}\n")

    result = subprocess.run(cmd)
    if result.returncode != 0:
        sys.exit(result.returncode)


if __name__ == "__main__":
    import fire

    fire.Fire({
        "record": main,
        "record-waa": cmd_record_waa,
        "annotate": cmd_annotate_waa,
        "eval": cmd_eval_dc,
    })
