"""``openadapt-flow encrypt`` and its hidden alias ``seal`` (for one release)."""

from __future__ import annotations

import argparse
from typing import Any, Callable


def register(sub: Any, handler: Callable[[argparse.Namespace], int]) -> None:
    """Add ``encrypt`` and the hidden ``seal`` alias, both bound to ``handler``."""
    for verb in ("encrypt", "seal"):
        p = sub.add_parser(
            verb,
            help=(
                "Encrypt every file of a bundle that can hold patient data with "
                "OPENADAPT_BUNDLE_KEY, bind the whole file set to one encrypted "
                "index, verify it, and publish it atomically at a new path"
            )
            if verb == "encrypt"
            else argparse.SUPPRESS,
            allow_abbrev=False,
        )
        p.add_argument("source", help="Existing workflow bundle directory")
        p.add_argument(
            "-o",
            "--out",
            required=True,
            help="New destination directory (must not already exist)",
        )
        p.set_defaults(func=handler)
