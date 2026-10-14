"""Verify the actor's operating-system authority boundary."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def probe_authority(
    trial_root: Path,
    actor_root: Path,
    fixture_root: Path,
    oracle_root: Path,
) -> dict:
    """Report path access from the process identity that will run the actor."""
    return {
        "schema_version": "openadapt.actor-authority-probe.v1",
        "process_uid": os.geteuid(),
        "checks": {
            "actor_root_writable": os.access(actor_root, os.W_OK),
            "trial_root_writable": os.access(trial_root, os.W_OK),
            "fixture_root_writable": os.access(fixture_root, os.W_OK),
            "fixture_store_writable": os.access(fixture_root / "effects.sqlite", os.W_OK),
            "oracle_root_readable": os.access(oracle_root, os.R_OK),
            "oracle_root_searchable": os.access(oracle_root, os.X_OK),
            "oracle_root_writable": os.access(oracle_root, os.W_OK),
            "truth_readable": os.access(oracle_root / "truth.json", os.R_OK),
            "observer_before_readable": os.access(oracle_root / "observer_before.json", os.R_OK),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trial-root", type=Path, required=True)
    parser.add_argument("--actor-root", type=Path, required=True)
    parser.add_argument("--fixture-root", type=Path, required=True)
    parser.add_argument("--oracle-root", type=Path, required=True)
    args = parser.parse_args()
    payload = probe_authority(args.trial_root, args.actor_root, args.fixture_root, args.oracle_root)
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
