"""Share recordings between computers using Magic Wormhole.

``send`` scrubs a copy of the recording first and sends only the copy (see
:mod:`openadapt_capture.share_scrub`). Sending the raw recording needs
``--unscrubbed --reason ...``, and that choice is recorded.

Usage:
    capture share send ./my_recording
    capture share send ./my_recording --unscrubbed --reason "synthetic demo"
    capture share receive 7-guitarist-revenge
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def _find_wormhole() -> str | None:
    """Find the wormhole executable path.

    On Windows after pip install, the executable may be in Python's Scripts/
    directory which isn't always on PATH.
    """
    # Check PATH first
    path = shutil.which("wormhole")
    if path:
        return path

    # Check in Python's Scripts directory (Windows) or bin directory (Unix)
    python_dir = Path(sys.executable).parent
    for candidate in [
        python_dir / "Scripts" / "wormhole.exe",  # Windows venv/global
        python_dir / "Scripts" / "wormhole",       # Windows without .exe
        python_dir / "wormhole",                   # Unix bin/
    ]:
        if candidate.exists():
            return str(candidate)

    return None


def _install_wormhole() -> str | None:
    """Attempt to install magic-wormhole.

    Returns:
        Path to wormhole executable if successful, None otherwise.
    """
    print("Installing magic-wormhole...")
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "magic-wormhole"],
            check=True,
            capture_output=True,
        )
        print("magic-wormhole installed")
    except subprocess.CalledProcessError as e:
        print(f"Failed to install magic-wormhole: {e}")
        return None

    # Find the newly installed binary
    path = _find_wormhole()
    if path:
        return path

    print("magic-wormhole installed but 'wormhole' command not found on PATH.")
    print(f"Try adding {Path(sys.executable).parent / 'Scripts'} to your PATH.")
    return None


def _ensure_wormhole() -> str | None:
    """Ensure magic-wormhole is available, install if needed.

    Returns:
        Path to wormhole executable, or None if unavailable.
    """
    path = _find_wormhole()
    if path:
        return path
    return _install_wormhole()


OPT_OUT_LEDGER = "share-unscrubbed.jsonl"
OPT_OUT_MARKER = "UNSCRUBBED_SHARE.json"


def opt_out_ledger_path() -> Path:
    """Local file where every unscrubbed share is recorded."""
    from openadapt_capture.ffmpeg_runtime import data_root

    return data_root() / OPT_OUT_LEDGER


def _record_opt_out(recording_path: Path, reason: str) -> dict:
    """Append an unscrubbed-share record to the local ledger and return it."""
    import getpass
    import json
    import time

    files = [f for f in recording_path.rglob("*") if f.is_file()]
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no login name in some sandboxes
        user = None
    record = {
        "schema": "openadapt.capture.share-unscrubbed/v1",
        "recorded_at": time.time(),
        "user": user,
        "recording": str(recording_path.resolve()),
        "file_count": len(files),
        "size_bytes": sum(f.stat().st_size for f in files),
        "reason": reason,
    }
    ledger = opt_out_ledger_path()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def _zip_folder(folder: Path, zip_path: Path, extra: dict | None = None) -> None:
    import json

    with ZipFile(zip_path, "w", ZIP_DEFLATED, compresslevel=6) as zf:
        for file in folder.rglob("*"):
            if file.is_file() and not file.is_symlink():
                zf.write(file, file.relative_to(folder.parent))
        if extra is not None:
            zf.writestr(
                f"{folder.name}/{OPT_OUT_MARKER}",
                json.dumps(extra, indent=2, sort_keys=True),
            )


def send(
    recording_dir: str,
    unscrubbed: bool = False,
    reason: str | None = None,
) -> str | None:
    """Send a recording via Magic Wormhole.

    By default the recording is scrubbed first: a scrubbed copy is built in a
    temporary folder and only that copy is sent. Scrubbing needs the
    ``privacy`` extra; without it nothing is sent.

    Args:
        recording_dir: Path to the recording directory.
        unscrubbed: Send the raw recording without scrubbing. Requires
            ``reason``. The choice is written to the local ledger and to an
            ``UNSCRUBBED_SHARE.json`` file inside the archive.
        reason: Why the raw recording must be sent (required with
            ``unscrubbed``).

    Returns:
        ``"sent"`` if the transfer finished, None otherwise.
    """
    from openadapt_capture.share_scrub import ShareScrubError, scrub_recording

    recording_path = Path(recording_dir)

    if not recording_path.exists():
        print(f"✗ Recording not found: {recording_path}")
        return None

    if not recording_path.is_dir():
        print(f"✗ Not a directory: {recording_path}")
        return None

    if unscrubbed and not (reason and str(reason).strip()):
        print("✗ --unscrubbed needs --reason explaining why the raw recording")
        print("  must leave this computer. The reason is recorded.")
        return None

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        opt_out: dict | None = None
        if unscrubbed:
            folder = recording_path
            opt_out = _record_opt_out(recording_path, str(reason).strip())
            print("! Sending the RAW recording without scrubbing.")
            print(f"  Recorded in {opt_out_ledger_path()}")
        else:
            print(f"Scrubbing a copy of {recording_path.name}...")
            try:
                folder = scrub_recording(recording_path, tmp / "scrubbed")
            except ShareScrubError as exc:
                print(f"✗ {exc}")
                print("  Nothing was sent. To send the raw recording anyway, run")
                print("  capture share send PATH --unscrubbed --reason '...'")
                return None
            _print_scrub_summary(folder)

        wormhole_path = _ensure_wormhole()
        if not wormhole_path:
            return None

        zip_path = tmp / f"{folder.name}.zip"
        print(f"Compressing {folder.name}...")
        _zip_folder(folder, zip_path, extra=opt_out)

        size_mb = zip_path.stat().st_size / (1024 * 1024)
        print(f"Compressed to {size_mb:.1f} MB")

        print("Sending via Magic Wormhole...")
        print("(Keep this window open until transfer completes)")
        print()

        try:
            subprocess.run(
                [wormhole_path, "send", str(zip_path)],
                check=True,
            )
            return "sent"
        except FileNotFoundError:
            print(f"'wormhole' command not found at: {wormhole_path}")
            print(f"Try: {sys.executable} -m pip install magic-wormhole")
            return None
        except subprocess.CalledProcessError as e:
            print(f"Wormhole send failed: {e}")
            return None
        except KeyboardInterrupt:
            print("\nCancelled")
            return None


def _print_scrub_summary(folder: Path) -> None:
    import json

    from openadapt_capture.share_scrub import MANIFEST_NAME

    manifest = json.loads((folder / MANIFEST_NAME).read_text(encoding="utf-8"))
    counts = manifest["counts"]
    print(
        f"Scrubbed {counts['text_values']} text values and "
        f"{counts['images']} images."
    )
    for item in manifest["withheld"]:
        print(f"  withheld {item['path']} ({item['reason']})")
    print("Scrubbing is automatic and can miss data. Review before sharing further.")


def receive(code: str, output_dir: str = ".") -> Path | None:
    """Receive a recording via Magic Wormhole.

    Args:
        code: The wormhole code (e.g., "7-guitarist-revenge").
        output_dir: Directory to save the recording (default: current dir).

    Returns:
        Path to the received recording directory, or None on failure.
    """
    wormhole_path = _ensure_wormhole()
    if not wormhole_path:
        return None

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        print(f"Receiving from wormhole code: {code}")

        try:
            subprocess.run(
                [wormhole_path, "receive", "--accept-file", "-o", str(tmpdir), code],
                check=True,
            )

            # Find the received zip file
            zip_files = list(tmpdir.glob("*.zip"))
            if not zip_files:
                print("✗ No zip file received")
                return None

            zip_path = zip_files[0]
            print(f"✓ Received {zip_path.name}")

            # Extract
            print("Extracting...")
            with ZipFile(zip_path, "r") as zf:
                zf.extractall(output_path)

            # Find the extracted directory
            extracted = [
                p for p in output_path.iterdir()
                if p.is_dir() and p.name != "__MACOSX"
            ]

            if extracted:
                recording_dir = extracted[0]
                print(f"✓ Saved to: {recording_dir}")
                return recording_dir
            else:
                print(f"✓ Extracted to: {output_path}")
                return output_path

        except subprocess.CalledProcessError as e:
            print(f"✗ Wormhole receive failed: {e}")
            return None
        except KeyboardInterrupt:
            print("\n✗ Cancelled")
            return None


def main() -> None:
    """CLI entry point for share commands."""
    import fire
    fire.Fire({
        "send": send,
        "receive": receive,
    })


if __name__ == "__main__":
    main()
