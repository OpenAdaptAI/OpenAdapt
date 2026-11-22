"""Measure how much of the synthetic PHI corpus the flight scrubber removes.

    python measure.py --seed 7 --count 50 --out results/2026-11-21-seed7.json

Two configurations are measured on the same corpus:

* ``presidio``: the openadapt-privacy scrubber alone (text scrub, image
  redaction), as the flight proxy used it before this change;
* ``flight``: what the proxy sends now, each text sent as a tool result (:class:`openadapt_flow.flight.scrub.Scrub`:
  URLs cut to their origin, Presidio, then the deterministic patterns; for
  images, Presidio redaction then the OCR pattern pass).

A field counts as leaked when its value, lower-cased with everything but
letters and digits removed, is a substring of the scrubbed output treated
the same way; a name also leaks when either part of it appears as a word.
For images the output is tesseract's reading of the scrubbed image, and only
fields tesseract could read in the original image are counted (the others
are reported as ``unreadable``).

Needs openadapt-privacy with Presidio, the spaCy model en_core_web_sm and
the tesseract binary. Synthetic data only.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from corpus import build, render_form  # noqa: E402

FIELDS = ("name", "dob", "mrn", "phone", "address")


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.lower())


def leaked(field: str, value: str, output: str) -> bool:
    if _norm(value) and _norm(value) in _norm(output):
        return True
    if field == "name":
        words = set(re.findall(r"[^\W\d_]+", output.lower()))
        return any(part.lower() in words for part in value.split() if len(part) > 1)
    return False


def _through_proxy(text: str, scrub: Any) -> str:
    """``text`` as the proxy sends it: a tool result in a request body (a
    JSON result is scrubbed leaf by leaf)."""
    from openadapt_flow.flight.scrub import scrub_request

    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t", "content": text}
                ],
            }
        ]
    }
    return scrub_request(body, scrub)["messages"][0]["content"][0]["content"]


def _ocr(png: bytes) -> str:
    import pytesseract
    from PIL import Image

    return pytesseract.image_to_string(Image.open(io.BytesIO(png)))


def run(seed: int, count: int) -> dict[str, Any]:
    from openadapt_flow.flight.scrub import Scrub, require_scrubber
    from openadapt_flow.privacy import scrub_outbound_image, scrub_outbound_text

    scrubber = require_scrubber()
    flight = Scrub(scrubber)
    configs = {
        "presidio": (
            lambda t: scrub_outbound_text(t, scrubber) or "",
            lambda png: scrub_outbound_image(png, scrubber),
        ),
        "flight": (
            lambda t: _through_proxy(t, flight),
            lambda png: flight.image(png)[0],
        ),
    }
    corpus = build(seed, count)
    results: dict[str, Any] = {}
    started = time.time()
    originals = {}
    for item in corpus:
        png = render_form_from(item)
        originals[item["index"]] = (png, _ocr(png))
    for name, (text_fn, image_fn) in configs.items():
        stats: dict[str, Any] = {
            "text": {f: {"total": 0, "leaked": 0} for f in FIELDS},
            "image": {f: {"readable": 0, "unreadable": 0, "leaked": 0} for f in FIELDS},
            "misses": [],
        }
        for item in corpus:
            record = item["record"]
            out = text_fn(item["text"])
            for field in FIELDS:
                stats["text"][field]["total"] += 1
                if leaked(field, record[field], out):
                    stats["text"][field]["leaked"] += 1
                    stats["misses"].append(
                        {"index": item["index"], "where": "text", "field": field}
                    )
            png, before = originals[item["index"]]
            after = _ocr(image_fn(png))
            for field in FIELDS:
                if not leaked(field, record[field], before):
                    stats["image"][field]["unreadable"] += 1
                    continue
                stats["image"][field]["readable"] += 1
                if leaked(field, record[field], after):
                    stats["image"][field]["leaked"] += 1
                    stats["misses"].append(
                        {"index": item["index"], "where": "image", "field": field}
                    )
        for where, key in (("text", "total"), ("image", "readable")):
            total = sum(v[key] for v in stats[where].values())
            missed = sum(v["leaked"] for v in stats[where].values())
            stats[where + "_recall"] = {
                "fields": total,
                "caught": total - missed,
                "recall": round((total - missed) / total, 4) if total else None,
            }
        results[name] = stats
    return {
        "schema": "openadapt.flight-scrub-recall/v1",
        "seed": seed,
        "records": count,
        "fields_per_record": list(FIELDS),
        "leak_rule": (
            "normalised value is a substring of the normalised output; a name "
            "also leaks when either part appears as a word"
        ),
        "seconds": round(time.time() - started, 1),
        "configs": results,
    }


def render_form_from(item: dict[str, Any]) -> bytes:
    from corpus import Record

    return render_form(Record(**item["record"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    data = run(args.seed, args.count)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    for name, stats in data["configs"].items():
        print(name, "text", stats["text_recall"], "image", stats["image_recall"])


if __name__ == "__main__":
    main()
