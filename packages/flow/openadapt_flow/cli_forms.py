"""Argument rewrites ``openadapt-flow`` applies before parsing."""

from __future__ import annotations

import argparse
from collections.abc import Callable


def qualify_default_form(
    argv: list[str], build_parser: Callable[[], argparse.ArgumentParser]
) -> list[str]:
    """``qualify <bundle> --verify ...`` means ``qualify campaign <bundle> ...``."""

    if len(argv) >= 2 and argv[0] == "qualify" and not argv[1].startswith("-"):
        verbs = set()
        for action in build_parser()._subparsers._group_actions:  # type: ignore[union-attr]
            qualify_parser = getattr(action, "choices", {}).get("qualify")
            if qualify_parser is not None:
                for sub_action in qualify_parser._subparsers._group_actions:  # type: ignore[union-attr]
                    verbs |= set(getattr(sub_action, "choices", {}) or {})
        if argv[1] not in verbs:
            return [argv[0], "campaign", *argv[1:]]
    return argv
