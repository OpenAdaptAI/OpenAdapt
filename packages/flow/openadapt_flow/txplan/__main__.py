"""``python -m openadapt_flow.txplan``: plan, apply, undo, and release agent writes."""

from __future__ import annotations

import sys

from openadapt_flow.txplan.cli import main

if __name__ == "__main__":
    sys.exit(main())
