"""CLI: regenerate the vision-hardening corpus + results, or print a summary.

Usage::

    # Re-run the sweep and WRITE benchmark/vision_hardening/{corpus,results}.json
    python -m openadapt_flow.validation.hardening --write

    # Just print the measured summary (no writes)
    python -m openadapt_flow.validation.hardening

    # Template tier only (fast, font-free, the CI ratchet tier)
    python -m openadapt_flow.validation.hardening --no-ocr --write

Regenerating is the ratcheting act: after a hardening fix that removes
silent-wrongs, run ``--write`` and commit the lowered ``ratchet_max_silent_wrong``
alongside the fix (a raise requires an explicit reviewed decision).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from openadapt_flow.validation.hardening import corpus as C


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="write corpus.json + results.json under benchmark/vision_hardening/",
    )
    parser.add_argument(
        "--no-ocr",
        action="store_true",
        help="template tier only (skip the slower, platform-variable OCR tier)",
    )
    args = parser.parse_args(argv)

    corpus_doc, results_doc = C.regenerate(include_ocr=not args.no_ocr)
    results_doc["generated_at"] = datetime.now(timezone.utc).isoformat()

    tt = results_doc["template_tier"]
    print(
        f"TEMPLATE tier (CI ratchet): N={tt['total']} "
        f"silent-wrong={tt['silent_wrong']} SWER={tt['silent_wrong_rate']:.3f} "
        f"| ratchet_max={corpus_doc['ratchet_max_silent_wrong']}"
    )
    print(f"  outcomes: {tt['counts']}")
    if "ocr_tier" in results_doc:
        ot = results_doc["ocr_tier"]
        print(
            f"OCR tier (informational): N={ot['total']} "
            f"silent-wrong={ot['silent_wrong']} SWER={ot['silent_wrong_rate']:.3f}"
        )

    if args.write:
        C.corpus_path().parent.mkdir(parents=True, exist_ok=True)
        C.corpus_path().write_text(
            json.dumps(corpus_doc, indent=2, sort_keys=True) + "\n"
        )
        C.results_path().write_text(
            json.dumps(results_doc, indent=2, sort_keys=True) + "\n"
        )
        print(f"wrote {C.corpus_path()}")
        print(f"wrote {C.results_path()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
