"""llms-full.txt, the review brief, matches the pages it is built from."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_llms_full_is_current() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_llms_full.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
