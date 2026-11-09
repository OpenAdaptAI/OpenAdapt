"""Digit-flanked reproduction harness for the name+DOB-primary identity fix
(7th wrong-patient reopening; see DENSE_SURFACE.md).

Renders + OCRs THREE corpora through the REAL dense pipeline
(openadapt_flow.validation.dense_surface) and reports false-accept (click
sibling) and false-abort (click true row), split by click config, so the
tradeoff is visible and reproducible:

  * adversarial   -- same name + same DOB, digit-flanked homoglyph MRN
                     (the review's attack3 shape; digit-body collapse the
                     6th-reopening letter-only flag missed). false accept
                     closes in click_name (identity rests solely on the
                     MRN); click_action is the disclosed residual.
  * realistic     -- DIFFERENT name/DOB siblings with a confusable-digit
                     MRN present on the target: name+DOB carry -> MUST
                     verify in click_action (no over-halt).
  * original      -- the shipped dense corpus build_collision_pairs
                     (false accept MUST stay 0 -- no regression).

Usage: python benchmark/dense_surface/digit_flanked_probe.py [seeds...]
"""
import sys
import random
from collections import defaultdict

import openadapt_flow.validation.dense_surface as ds
from openadapt_flow.validation.dense_surface import (
    CollisionPair, Row, _dob, _mrn, run_trials,
)

SURN, FIRST = ds._SURNAMES, ds._FIRSTS

# digit-flanked homoglyph forms (review attack3)
FLANK_FORMS = [
    ("O0f_MG", "MG44O8", "O", "0"),
    ("O0f_AC", "AC5OO61", "O", "0"),
    ("O0f_KJ", "KJ77OO", "O", "0"),
    ("l1f_MG", "MG44l8", "l", "1"),
    ("I1f_MG", "MG44I8b", "I", "1"),
    ("l1f_KJ", "KJ77ll", "l", "1"),
]


def adversarial_pairs(seed):
    """Same name + same DOB; only the digit-flanked homoglyph MRN differs."""
    rng = random.Random(seed * 13 + 2)
    pairs = []
    for label, sib_letterform, letter, digit in FLANK_FORMS:
        target_mrn = sib_letterform.replace(letter, digit)  # genuine digits
        sibling_mrn = sib_letterform
        dob = _dob(rng)
        name = f"{rng.choice(SURN)}, {rng.choice(FIRST)}"
        sex = rng.choice("MF")
        status = rng.choice(ds._STATUSES)
        pairs.append(CollisionPair(
            label,
            Row(name, dob, target_mrn, sex, status),
            Row(name, dob, sibling_mrn, sex, status),
            f"same name+DOB; digit-flanked MRN ({target_mrn} vs {sibling_mrn})"))
    return pairs


def realistic_pairs(seed):
    """DIFFERENT patients (the common real case): siblings differ in NAME
    and/or DOB, while a confusable-glyph MRN is present -- the TARGET's own
    MRN is a realistic <alpha prefix><digit body> that CONTAINS glyph-
    vulnerable digits 0/1 (the confusable glyphs), exactly as ~3 of 4 real
    MRNs do. name+DOB discriminate, so these MUST verify on the true row and
    mismatch on the sibling; the confusable-digit MRN must NOT block."""
    rng = random.Random(seed * 17 + 5)
    # realistic MRNs whose digit body carries 0/1 (glyph-vulnerable digits)
    forms = ["MG480312", "RC710284", "PT601925", "KJ770019", "AC500614",
             "MG140408"]
    pairs = []
    for i, tmrn in enumerate(forms):
        # target and sibling are DIFFERENT people (different name; half also
        # differ in DOB) -- the common real case.
        tname = f"{rng.choice(SURN)}, {rng.choice(FIRST)}"
        sname = f"{rng.choice(SURN)}, {rng.choice(FIRST)}"
        while sname == tname:
            sname = f"{rng.choice(SURN)}, {rng.choice(FIRST)}"
        tdob = _dob(rng)
        sdob = _dob(rng) if i % 2 == 0 else tdob
        sex = rng.choice("MF")
        status = rng.choice(ds._STATUSES)
        smrn = _mrn(rng, prefix=rng.choice(["MG", "RC", "PT"]))
        pairs.append(CollisionPair(
            f"realistic_{i}",
            Row(tname, tdob, tmrn, sex, status),
            Row(sname, sdob, smrn, sex, status),
            f"different name/DOB; target confusable-digit MRN {tmrn}"))
    return pairs


def measure(name, builder, seeds):
    ds.build_collision_pairs = builder
    res = run_trials(seeds, n_rows=40, progress=False)
    tr = res["trials"]
    by_cfg = defaultdict(lambda: {"fa": 0, "fab": 0, "n": 0})
    fa_examples = []
    for t in tr:
        c = by_cfg[t["click_config"]]
        c["n"] += 1
        if t["is_false_accept"]:
            c["fa"] += 1
            fa_examples.append(t)
        if t["is_false_abort"]:
            c["fab"] += 1
    total_fa = sum(t["is_false_accept"] for t in tr)
    total_fab = sum(t["is_false_abort"] for t in tr)
    n = len(tr)
    print(f"\n===== {name}: {n} trials =====")
    print(f"  FALSE ACCEPT: {total_fa}/{n} ({100*total_fa/n:.1f}%)   "
          f"FALSE ABORT: {total_fab}/{n} ({100*total_fab/n:.1f}%)")
    for cfg in ("click_name", "click_action"):
        c = by_cfg[cfg]
        if c["n"]:
            print(f"    {cfg:13} FA {c['fa']:3}/{c['n']:3}  "
                  f"FABORT {c['fab']:3}/{c['n']:3}")
    if fa_examples:
        print("  -- sample FALSE ACCEPTS --")
        for t in fa_examples[:4]:
            print(f"    {t['click_config']} {t['collision_class']} "
                  f"tgt={t['target_mrn']} sib={t['sibling_mrn']}")
            print(f"      rec={t['acc_expected']!r}")
            print(f"      obs={t['acc_observed']!r}")
    return total_fa, total_fab, n


if __name__ == "__main__":
    seeds = [int(x) for x in sys.argv[1:]] or [1, 2]
    print(f"seeds={seeds}")
    original_builder = ds.build_collision_pairs
    measure("ADVERSARIAL (same name+DOB, digit-flanked)", adversarial_pairs, seeds)
    measure("REALISTIC (diff name/DOB, confusable MRN)", realistic_pairs, seeds)
    measure("ORIGINAL dense corpus", original_builder, seeds)
