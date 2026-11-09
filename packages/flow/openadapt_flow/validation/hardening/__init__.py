"""Systematic adversarial hardening flywheel for the vision resolution ladder.

This package is the first increment of a *continuous-hardening flywheel*
(design: ``~/oa/src/.private/hardening_flywheel_2026_07_20.md``). It
systematically GENERATES the failure space over the pixel/no-DOM resolution
ladder, runs the REAL resolver + vision against each generated case, and
CLASSIFIES the outcome as one of ``correct`` / ``safe-halt`` / ``over-halt`` /
``silent-wrong`` — where **silent-wrong** (a confident click on the *wrong*
target that no downstream guard on an unarmed step would catch) is the
dangerous class the product is sold against.

The load-bearing idea is a **metamorphic relation**: every perturbation applied
to the live frame *preserves the true target's identity and location*, so a
correct resolver must either stay on the true target or HALT — it must **never**
resolve to a different (look-alike) instance. Any perturbation that flips the
resolved target onto a decoy is, by construction, a silent-wrong-resolution.

Public surface:

- :mod:`~openadapt_flow.validation.hardening.fixtures` — real-glyph, PIL-only
  fixture families (repeated icons, labeled rows, duplicate buttons, form
  fields, glyph-collapse MRN cells). No browser; deterministic; CI-cheap.
- :mod:`~openadapt_flow.validation.hardening.perturbations` — the parameterized,
  metamorphic perturbation space (DPI, theme inversion, JPEG/codec compression,
  sub-pixel jitter, occlusion/cursor/tooltip, blur, colour-depth, local
  mid-run drift, and compositions).
- :mod:`~openadapt_flow.validation.hardening.harness` — runs the real
  ``resolver.resolve`` on each ``(fixture, perturbation)`` case, classifies the
  outcome, sweeps the grid, runs a bounded seeded ADVERSARIAL SEARCH that hunts
  for confident-wrong cases, and summarizes into a silent-wrong rate.
- :mod:`~openadapt_flow.validation.hardening.corpus` — the growing, committed
  failure corpus (every discovered silent-wrong becomes a frozen regression
  case) and the monotone-improvement RATCHET.

Nothing here modifies the resolver or weakens any safety guard: it only
*measures* the ladder and *locks in* that the silent-wrong rate can only go
down.
"""

from __future__ import annotations

from openadapt_flow.validation.hardening.harness import (
    Outcome,
    ResultRow,
    classify_case,
    summarize,
    sweep,
)

__all__ = [
    "Outcome",
    "ResultRow",
    "classify_case",
    "summarize",
    "sweep",
]
