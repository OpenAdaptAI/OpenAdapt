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

from corpus import (  # noqa: E402
    LONG_PREAMBLE,
    REASONS,
    UI_WORDS,
    Record,
    build,
    render_form,
    ui_instruction,
    ui_screen,
)

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


def _as_message(text: str, scrub: Any) -> str:
    """``text`` as the proxy sends a plain user message text block."""
    from openadapt_flow.flight.scrub import scrub_request

    body = {"messages": [{"role": "user", "content": [{"type": "text", "text": text}]}]}
    return scrub_request(body, scrub)["messages"][0]["content"][0]["text"]


def _ocr(png: bytes) -> str:
    import pytesseract
    from PIL import Image

    return pytesseract.image_to_string(Image.open(io.BytesIO(png)))


def run(seed: int, count: int) -> dict[str, Any]:
    from openadapt_flow.flight.pseudonym import Pseudonyms
    from openadapt_flow.flight.scrub import Scrub, require_scrubber
    from openadapt_flow.privacy import scrub_outbound_image, scrub_outbound_text

    scrubber = require_scrubber()
    base = Scrub(scrubber)

    def fresh() -> Scrub:
        """A new run's scrubber (its own token map), sharing the NER memo."""
        scrub = Scrub(scrubber, pseudonyms=Pseudonyms())
        scrub._ner = base._ner
        return scrub

    def presidio_text(t: str) -> str:
        return scrub_outbound_text(t, scrubber) or ""

    # Text paths: "text" is the corpus text sent as a tool result (the
    # 2026-11-21 measurement); "text_message" the same text as plain message
    # text (a JSON text is then one string); "text_long" that message after
    # a long agent preamble (LONG_PREAMBLE), as a computer-use loop sends it.
    configs = {
        "presidio": (
            {
                "text": presidio_text,
                "text_message": presidio_text,
                "text_long": lambda t: presidio_text(LONG_PREAMBLE + t),
            },
            lambda png: scrub_outbound_image(png, scrubber),
        ),
        "flight": (
            {
                "text": lambda t: _through_proxy(t, fresh()),
                "text_message": lambda t: _as_message(t, fresh()),
                "text_long": lambda t: _as_message(LONG_PREAMBLE + t, fresh()),
            },
            lambda png: fresh().image(png)[0],
        ),
    }
    corpus = build(seed, count)
    results: dict[str, Any] = {}
    started = time.time()
    originals = {}
    for item in corpus:
        png = render_form_from(item)
        originals[item["index"]] = (png, _ocr(png))
    for name, (text_fns, image_fn) in configs.items():
        stats: dict[str, Any] = {
            path: {f: {"total": 0, "leaked": 0} for f in FIELDS} for path in text_fns
        }
        stats["image"] = {
            f: {"readable": 0, "unreadable": 0, "leaked": 0} for f in FIELDS
        }
        stats["misses"] = []
        for item in corpus:
            record = item["record"]
            for path, text_fn in text_fns.items():
                out = text_fn(item["text"])
                for field in FIELDS:
                    stats[path][field]["total"] += 1
                    if leaked(field, record[field], out):
                        stats[path][field]["leaked"] += 1
                        stats["misses"].append(
                            {"index": item["index"], "where": path, "field": field}
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
        wheres = [(path, "total") for path in text_fns] + [("image", "readable")]
        for where, key in wheres:
            total = sum(v[key] for v in stats[where].values())
            missed = sum(v["leaked"] for v in stats[where].values())
            stats[where + "_recall"] = {
                "fields": total,
                "caught": total - missed,
                "recall": round((total - missed) / total, 4) if total else None,
            }
        results[name] = stats
    precision = _precision(corpus, configs, scrubber, fresh)
    return {
        "precision": precision,
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


def _words_kept(words: list[str], text: str) -> list[str]:
    found = set(re.findall(r"[a-z0-9-]+", text.lower()))
    return [w for w in words if w.lower() in found]


def _precision(
    corpus: list[dict[str, Any]], configs: dict[str, Any], scrubber: Any, fresh: Any
) -> dict[str, Any]:
    """How much that is not personal data stays readable.

    For the first 20 records: a work-list screenshot (app name, navigation,
    headers, the patient's row with a referral reason and a priority) and a
    task instruction naming the patient. Counted: the non-PHI words
    tesseract reads on the original screen (UI_WORDS and the reason's
    words), and the fraction of them it still reads after scrubbing; the
    instruction's non-PHI words, and the fraction kept. The patient's name
    and record number on the same screen are counted too.
    """
    out: dict[str, Any] = {}
    items = corpus[:20]
    for name, (text_fns, image_fn) in configs.items():
        screen = {"ui_words": 0, "kept": 0, "phi": 0, "phi_hidden": 0}
        text = {"words": 0, "kept": 0, "phi": 0, "phi_hidden": 0}
        lost: dict[str, int] = {}
        for i, item in enumerate(items):
            record = Record(**item["record"])
            reason = REASONS[i % len(REASONS)]
            ui = UI_WORDS + reason.split()
            png = ui_screen(record, reason)
            before = _ocr(png)
            readable = _words_kept(ui, before)
            after = _ocr(image_fn(png))
            kept = _words_kept(readable, after)
            screen["ui_words"] += len(readable)
            screen["kept"] += len(kept)
            for w in set(readable) - set(kept):
                lost[w] = lost.get(w, 0) + 1
            for field in ("name", "mrn"):
                if leaked(field, item["record"][field], before):
                    screen["phi"] += 1
                    screen["phi_hidden"] += not leaked(
                        field, item["record"][field], after
                    )
            instruction = ui_instruction(record, reason)
            plain = [
                w
                for w in re.findall(r"[A-Za-z-]+", instruction)
                if w not in record.name.split() and w != "MRN"
            ]
            scrubbed = text_fns["text_message"](instruction)
            text["words"] += len(plain)
            text["kept"] += len(_words_kept(plain, scrubbed))
            for field in ("name", "mrn"):
                text["phi"] += 1
                text["phi_hidden"] += not leaked(field, item["record"][field], scrubbed)
        for part in (screen, text):
            total = part.get("ui_words", part.get("words"))
            part["kept_fraction"] = round(part["kept"] / total, 4) if total else None
        out[name] = {
            "screen": screen,
            "instruction": text,
            "screen_words_lost": dict(sorted(lost.items())),
        }
    return out


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
        print(name, {k: v for k, v in stats.items() if k.endswith("_recall")})
        print("  precision", json.dumps(data["precision"][name]))


if __name__ == "__main__":
    main()
