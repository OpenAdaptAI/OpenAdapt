"""``python -m openadapt_flow.hooks``: the entry the hook launcher runs."""

from __future__ import annotations

import sys

from openadapt_flow.hooks.cli import main

if __name__ == "__main__":
    sys.exit(main())
