"""Write docs/COMPATIBILITY.md from openadapt_flow/compatibility.py.

Usage (from packages/flow): python scripts/build_compatibility_doc.py
``--check`` exits 1 when the file is out of date instead of writing it.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from openadapt_flow.compatibility import render_markdown  # noqa: E402

TARGET = ROOT / "docs" / "COMPATIBILITY.md"


def main(argv: list[str]) -> int:
    text = render_markdown()
    if "--check" in argv:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != text:
            print(
                f"{TARGET} is out of date: run python scripts/build_compatibility_doc.py"
            )
            return 1
        return 0
    TARGET.write_text(text, encoding="utf-8")
    print(f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
