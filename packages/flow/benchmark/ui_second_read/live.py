"""Run the pre-registered UI second-read cases on live OpenEMR or Dolibarr.

See ``PREREG.md`` (committed before the first run). The acting session is a
headless Playwright browser that writes through the app's forms. The verifier
is the shipped ``ui_readback`` verifier with its real Playwright driver; its
deployment has no REST, SQL or FHIR section, and its session aborts requests to
the apps' API paths. SQL is used only to seed records and plant faults.

Credentials come from the environment (synthetic fixture values). With
``OPENADAPT_LAB_CHAOS=<profile>`` every URL goes through the lab chaos proxy.

    python -m benchmark.ui_second_read.live --app dolibarr --out results/
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Optional

from benchmark.ui_second_read.common import NoApiDriver, profile


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--app", choices=["openemr", "dolibarr"], required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args(argv)
    if args.app == "openemr":
        from benchmark.ui_second_read import app_openemr as mod
    else:
        from benchmark.ui_second_read import app_dolibarr as mod
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{args.app}-{profile()}.jsonl"
    with mod.App(out) as app, path.open("a", encoding="utf-8") as sink:
        for case_id, fault, predicted, run in mod.CASES:
            if args.only and case_id not in args.only:
                continue
            started = time.monotonic()
            try:
                result: dict[str, Any] = run(app)
            except Exception as exc:  # noqa: BLE001 - a harness error is a row
                result = {"verdict": "harness_error", "reason": repr(exc)[:400]}
            row = {
                "app": args.app,
                "network": profile(),
                "case": case_id,
                "fault": fault,
                "predicted": predicted,
                "wall_seconds": round(time.monotonic() - started, 2),
                **result,
            }
            sink.write(json.dumps(row) + "\n")
            sink.flush()
            print(json.dumps(row)[:400], flush=True)
    print(json.dumps({"api_requests_blocked": NoApiDriver.blocked}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
