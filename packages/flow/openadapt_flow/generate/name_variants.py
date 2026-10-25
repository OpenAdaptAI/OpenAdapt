"""A generic table of given-name variants (nicknames, diminutives, short forms).

Duplicate rules need to know that Bob and Robert can be one person. That is
general knowledge about names, not about any system's patients, so the table
is generated once, by a model asked only for common given names and their
variants. No record, and no name from any system, goes into the request.
The result is committed (``name_variants.json``) and read deterministically:
a run never calls a model.

    python -m openadapt_flow.generate.name_variants --out name_variants.json

The table is a candidate signal, not a rule: ``generate/identity.py`` admits
the clauses that use it only within its false-stop budget, and the review
shows the person that a model-made table was used.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

TABLE = Path(__file__).with_name("name_variants.json")
SCHEMA = "openadapt.name-variants/v1"

PROMPT = (
    "List the 400 most common given names in English-speaking countries "
    "(both men's and women's), and for each its common nicknames, "
    "diminutives and short forms (for example Robert: Bob, Bobby, Rob, Robbie, "
    "Bert). Include a name's common variants only, not rare ones. Answer with "
    "JSON only, no prose: an object mapping each given name to a list of its "
    "variants, all in Title Case."
)

#: USD per million tokens (input, output), for the cost record.
PRICES = {"claude-sonnet-5-5": (3.0, 15.0), "claude-opus-5-5": (15.0, 75.0)}


@lru_cache(maxsize=1)
def load(path: str = str(TABLE)) -> dict[str, frozenset[str]]:
    """Name -> its variants, both ways, casefolded. Empty when absent."""

    p = Path(path)
    if not p.is_file():
        return {}
    doc = json.loads(p.read_text())
    out: dict[str, set[str]] = {}
    for name, variants in (doc.get("variants") or {}).items():
        key = name.casefold()
        for v in variants:
            v = str(v).casefold()
            if not v or v == key:
                continue
            out.setdefault(key, set()).add(v)
            out.setdefault(v, set()).add(key)
    return {k: frozenset(v) for k, v in out.items()}


def are_variants(
    a: str, b: str, table: dict[str, frozenset[str]] | None = None
) -> bool:
    table = load() if table is None else table
    x, y = a.casefold().strip(), b.casefold().strip()
    return bool(x and y and x != y and y in table.get(x, frozenset()))


def generate(model: str) -> tuple[dict[str, list[str]], dict[str, Any]]:
    """Ask ``model`` for the table. Returns (variants, usage record)."""

    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    with client.messages.stream(
        model=model,
        max_tokens=32000,
        messages=[{"role": "user", "content": PROMPT}],
    ) as stream:
        response = stream.get_final_message()
    text = "".join(getattr(b, "text", "") for b in response.content).strip()
    start, end = text.find("{"), text.rfind("}")
    raw = json.loads(text[start : end + 1])
    variants = {
        str(k).strip(): sorted({str(v).strip() for v in vs if str(v).strip()})
        for k, vs in raw.items()
        if isinstance(vs, list) and str(k).strip()
    }
    price_in, price_out = PRICES.get(model, (15.0, 75.0))
    usage = {
        "model": model,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cost_usd": round(
            response.usage.input_tokens * price_in / 1e6
            + response.usage.output_tokens * price_out / 1e6,
            6,
        ),
    }
    return variants, usage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default="claude-sonnet-5-5")
    parser.add_argument("--out", type=Path, default=TABLE)
    args = parser.parse_args(argv)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set", file=sys.stderr)
        return 2
    variants, usage = generate(args.model)
    args.out.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA,
                "generated_by": usage["model"],
                "prompt": PROMPT,
                "usage": usage,
                "variants": dict(sorted(variants.items())),
            },
            indent=1,
            sort_keys=False,
        )
        + "\n"
    )
    print(f"{len(variants)} names, ${usage['cost_usd']:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
