"""Share recordings between computers using Magic Wormhole.

Usage:
    capture share send ./my_recording
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


def send(recording_dir: str) -> str | None:
    """Send a recording via Magic Wormhole.

    Args:
        recording_dir: Path to the recording directory.

    Returns:
        The wormhole code if successful, None otherwise.
    """
    recording_path = Path(recording_dir)

    if not recording_path.exists():
        print(f"✗ Recording not found: {recording_path}")
        return None

    if not recording_path.is_dir():
        print(f"✗ Not a directory: {recording_path}")
        return None

    wormhole_path = _ensure_wormhole()
    if not wormhole_path:
        return None

    # Create a temporary zip file
    zip_name = f"{recording_path.name}.zip"

    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = Path(tmpdir) / zip_name

        print(f"Compressing {recording_path.name}...")
        with ZipFile(zip_path, "w", ZIP_DEFLATED, compresslevel=6) as zf:
            for file in recording_path.rglob("*"):
                if file.is_file():
                    arcname = file.relative_to(recording_path.parent)
                    zf.write(file, arcname)

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
