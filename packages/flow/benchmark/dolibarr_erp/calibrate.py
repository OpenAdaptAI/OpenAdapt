"""Calibrate the noise profile: what changes in the record when a user only reads?

Signs in, opens a fixed list of read-only pages (lists, cards, reports),
signs out, and diffs the whole record against the baseline. Repeated
``--n`` times from a fresh reset. Every change seen is printed; the
columns that change on every read-only session are what ``noise.json``
may excuse, and nothing else.

    python -m benchmark.dolibarr_erp.calibrate --n 3
"""

from __future__ import annotations

import argparse
import json
from collections import Counter

from benchmark.dolibarr_erp import fixture, hooks
from openadapt_flow.rl_env.record_grader import diff

PAGES = [
    "/index.php", "/societe/list.php", "/societe/card.php?socid=1", "/contact/list.php",
    "/compta/facture/list.php", "/compta/facture/card.php?facid=2", "/commande/list.php",
    "/comm/propal/list.php", "/product/list.php", "/product/card.php?id=1",
    "/product/stock/list.php", "/compta/bank/list.php", "/projet/list.php",
    "/categories/index.php?type=2", "/compta/facture/list.php?search_status=1",
    "/societe/card.php?socid=3&action=edit",  # an edit form opened and left
]


def session(n_pages: list[str]) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        page = b.new_context(viewport={"width": 1280, "height": 800}).new_page()
        hooks.prepare(page, fixture.app_url())
        for p in n_pages:
            page.goto(fixture.app_url() + p, wait_until="load")
        page.goto(fixture.app_url() + "/user/logout.php", wait_until="load")
        b.close()


def main() -> None:
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("--n", type=int, default=3)
    a = ap.parse_args()
    seen: Counter[str] = Counter()
    for i in range(a.n):
        fixture.reset()
        before = fixture.read_record()
        session(PAGES)
        after = fixture.read_record()
        changes = diff(before, after)
        for ch in changes:
            d = ch.as_dict()
            tag = f"{ch.table} {ch.op} " + (",".join(ch.columns) if ch.op == "update" else "")
            seen[tag] += 1
            print(i, json.dumps(d, default=str)[:300])
    print("\nchange kinds over", a.n, "sessions:")
    for k, v in seen.most_common():
        print(f"  {v}x {k}")


if __name__ == "__main__":
    main()
