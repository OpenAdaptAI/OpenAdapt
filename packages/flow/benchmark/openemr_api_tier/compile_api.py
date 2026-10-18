"""Step 2: compile the captured referral save into an API-tier bundle.

    python -m benchmark.openemr_api_tier.compile_api --har <referral.har> --gui-bundle <dir> --out <dir>
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from benchmark.openemr_api_tier.formtier import compile_from_har


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--har", required=True)
    ap.add_argument("--gui-bundle", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    capture = Path(a.har).with_name("capture.json")
    run_params = json.loads(capture.read_text()).get("params") if capture.exists() else None
    print(json.dumps(compile_from_har(Path(a.har), Path(a.gui_bundle), Path(a.out), run_params), indent=1))


if __name__ == "__main__":
    main()
