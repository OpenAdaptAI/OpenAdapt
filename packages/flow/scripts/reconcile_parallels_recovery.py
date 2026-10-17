#!/usr/bin/env python3
"""Recover one exact Parallels validation VM from its durable journal."""

from __future__ import annotations

import argparse

from openadapt_flow.backends.parallels_vm import recover_parallels_vm


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--journal",
        required=True,
        help="Absolute host path to the private recovery journal",
    )
    args = parser.parse_args()
    recovered = recover_parallels_vm(args.journal)
    print("recovered exact Parallels base" if recovered else "no recovery pending")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
