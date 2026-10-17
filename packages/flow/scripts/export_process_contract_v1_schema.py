#!/usr/bin/env python3
"""Export the public ProcessContract v1 JSON Schema."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from openadapt_flow.admitted_composition import ProcessContractV1

    destination = root / "schemas" / "process-contract-v1.json"
    destination.write_text(
        json.dumps(ProcessContractV1.model_json_schema(), indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
