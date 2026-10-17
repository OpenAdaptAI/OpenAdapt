"""Structured-text (DOM) identity path vs the OCR name+DOB-primary fallback,
measured side by side on the real dense render->OCR pipeline (feat/dom-identity).

An adversarial review PROVED the OCR-only identity path cannot close the
same-name/same-DOB glyph-collapse case: a sibling MRN one glyph apart
(MG4408 vs MG44O8, AC50061 vs AC5OO61) renders to a BYTE-IDENTICAL OCR band,
so the matcher sees the same input a legit re-read produces and cannot tell
the two patients apart. This probe shows the escape: verify identity against
STRUCTURED text (the DOM row text -- backend.structured_text_at) where the
backend exposes it, and fall back to OCR only on pure-pixel substrates.

For each of three corpora it reports, per click config:

  * STRUCTURED (DOM) path -- false accept (sibling verifies) and false abort
    (true row refused). Expected: 0 / 0 -- the DOM MRN chars are distinct, so
    the sibling mismatches and the true row verifies, with no OCR-availability
    (over-halt) cost, on the digit-flanked attack AND the original corpus.
  * OCR FALLBACK path (#27) -- the same numbers the shipped matcher produces
    when structured text is unavailable (simulated by ignoring the DOM):
    0 FA on the original corpus, the disclosed digit-flanked residual, and the
    halt cost. Confirms no regression to the pixel-only path.

Usage: python benchmark/dense_surface/dom_identity_probe.py [seeds...]
"""
import sys
from collections import defaultdict

import openadapt_flow.validation.dense_surface as ds
from openadapt_flow.validation.dense_surface import run_trials
from digit_flanked_probe import adversarial_pairs, realistic_pairs


def measure(name, builder, seeds):
    original = ds.build_collision_pairs
    if builder is not None:
        ds.build_collision_pairs = builder
    try:
        res = run_trials(seeds, n_rows=40, progress=False)
    finally:
        ds.build_collision_pairs = original
    tr = res["trials"]
    ocr = defaultdict(lambda: {"fa": 0, "fab": 0, "n": 0})
    dom = defaultdict(lambda: {"fa": 0, "fab": 0, "n": 0, "armed": 0})
    dom_collapse = []
    for t in tr:
        co = ocr[t["click_config"]]
        co["n"] += 1
        co["fa"] += bool(t["is_false_accept"])
        co["fab"] += bool(t["is_false_abort"])
        cd = dom[t["click_config"]]
        cd["n"] += 1
        cd["armed"] += bool(t.get("structured_armed"))
        cd["fa"] += bool(t.get("is_structured_false_accept"))
        cd["fab"] += bool(t.get("is_structured_false_abort"))
        # glyph-collapse witness: OCR bands raw-identical but DOM strings differ
        if t["is_false_accept"] and not t.get("is_structured_false_accept"):
            dom_collapse.append(t)

    def totals(d):
        fa = sum(c["fa"] for c in d.values())
        fab = sum(c["fab"] for c in d.values())
        n = sum(c["n"] for c in d.values())
        return fa, fab, n

    ofa, ofab, n = totals(ocr)
    dfa, dfab, _ = totals(dom)
    print(f"\n===== {name}: {n} trials =====")
    print(f"  STRUCTURED (DOM)  FALSE ACCEPT {dfa}/{n} "
          f"({100*dfa/n:.1f}%)   FALSE ABORT {dfab}/{n} ({100*dfab/n:.1f}%)")
    print(f"  OCR FALLBACK #27  FALSE ACCEPT {ofa}/{n} "
          f"({100*ofa/n:.1f}%)   FALSE ABORT {ofab}/{n} ({100*ofab/n:.1f}%)")
    for cfg in ("click_name", "click_action"):
        cd, co = dom[cfg], ocr[cfg]
        if co["n"]:
            print(f"    {cfg:13}  DOM FA {cd['fa']:3}/{cd['n']:3} "
                  f"FABORT {cd['fab']:3}/{cd['n']:3} (armed {cd['armed']})"
                  f"   |   OCR FA {co['fa']:3}/{co['n']:3} "
                  f"FABORT {co['fab']:3}/{co['n']:3}")
    if dom_collapse:
        print("  -- glyph-collapse cases the DOM path CAUGHT that OCR missed --")
        for t in dom_collapse[:3]:
            print(f"    {t['click_config']} {t['collision_class']} "
                  f"tgt={t['target_mrn']} sib={t['sibling_mrn']}")
            print(f"      OCR  rec band={t['acc_expected']!r}")
            print(f"      OCR  obs band={t['acc_observed']!r}  (raw-identical)")
            print(f"      DOM  rec={t['structured_recorded']!r}")
            print(f"      DOM  sib={t['structured_sibling_live']!r}  (DISTINCT)")
    return dfa, dfab, ofa, ofab, n


if __name__ == "__main__":
    seeds = [int(x) for x in sys.argv[1:]] or [1, 2]
    print(f"seeds={seeds}")
    measure("ADVERSARIAL (same name+DOB, digit-flanked)", adversarial_pairs, seeds)
    measure("REALISTIC (diff name/DOB, confusable MRN)", realistic_pairs, seeds)
    measure("ORIGINAL dense corpus", None, seeds)
