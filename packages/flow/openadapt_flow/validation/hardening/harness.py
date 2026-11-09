"""Run the REAL resolver on generated cases and classify the outcome.

This is the engine of the flywheel. For each ``(fixture, perturbation)`` case
it:

1. applies the metamorphic perturbation to the fixture's clean frame,
2. runs the **unmodified** :func:`openadapt_flow.runtime.resolver.resolve`
   with the real :mod:`openadapt_flow.vision` (real cv2 template matcher +
   real OCR) — never a fake,
3. classifies the outcome against the exact ground truth:

   - **correct** — resolved within tolerance of the (mapped) true target,
   - **over-halt** — HALT under a *legible* (mild) perturbation: the target was
     still recoverable, so this is an availability loss (safe),
   - **safe-halt** — HALT under a *severe* (illegible) perturbation: a correct
     refusal,
   - **silent-wrong** — resolved a point that is NOT the true target (it landed
     on a look-alike decoy, or off in space): the dangerous class.

The silent-wrong rate (SWER, borrowed from the EffectBench taxonomy) is the
headline. The sweep and a bounded seeded ADVERSARIAL SEARCH both feed the same
classifier, and every silent-wrong is emitted as a frozen regression case.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import openadapt_flow.vision as vision
from openadapt_flow.ir import Point
from openadapt_flow.runtime.resolver import resolve
from openadapt_flow.validation.hardening import fixtures as fx
from openadapt_flow.validation.hardening import perturbations as pt
from openadapt_flow.validation.hardening.fixtures import Fixture
from openadapt_flow.validation.hardening.perturbations import Perturbation


class Outcome(str, Enum):
    """The four resolution outcomes (silent-wrong is the dangerous one)."""

    CORRECT = "correct"
    OVER_HALT = "over-halt"
    SAFE_HALT = "safe-halt"
    SILENT_WRONG = "silent-wrong"


@dataclass(frozen=True)
class ResultRow:
    """One classified ``(fixture, perturbation)`` case."""

    fixture_key: str
    family: str
    fixture_params: dict
    perturbation: str
    perturbation_params: dict
    severity: str
    outcome: Outcome
    rung: Optional[str]
    confidence: Optional[float]
    # Distance from the resolved point to the true target (mapped), in perturbed
    # pixels; and to the nearest decoy (both None on HALT).
    dist_true: Optional[float]
    dist_nearest_decoy: Optional[float]
    landed_on_decoy: bool

    def corpus_entry(self) -> dict:
        """The frozen regression record for a silent-wrong case."""
        return {
            "family": self.family,
            "fixture_params": self.fixture_params,
            "perturbation": self.perturbation,
            "perturbation_params": self.perturbation_params,
            "rung": self.rung,
            "confidence": None
            if self.confidence is None
            else round(self.confidence, 4),
            "dist_true": None if self.dist_true is None else round(self.dist_true, 1),
            "landed_on_decoy": self.landed_on_decoy,
        }


def _dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def classify_case(fixture: Fixture, perturbation: Perturbation) -> ResultRow:
    """Apply one perturbation, run the real resolver, and classify the outcome.

    The true target and decoys are mapped through the perturbation's affine so
    ground truth stays exact under DPI/jitter.
    """
    res_p = perturbation.apply(fixture.clean_png, fixture.viewport, fixture.true_center)
    live_png = res_p.png
    viewport = res_p.viewport
    true_pt = res_p.map_point(fixture.true_center)
    decoys = [res_p.map_point(d) for d in fixture.decoys]
    tol = fixture.tolerance_px * max(1.0, res_p.scale)

    resolved = resolve(
        fixture.anchor,
        live_png,
        vision,
        template_png=fixture.template_png,
        viewport=viewport,
    )

    if resolved is None:
        halt = (
            Outcome.OVER_HALT
            if perturbation.severity in ("none", "mild")
            else Outcome.SAFE_HALT
        )
        return ResultRow(
            fixture_key=fixture.key(),
            family=fixture.family,
            fixture_params=fixture.params,
            perturbation=perturbation.name,
            perturbation_params=perturbation.params,
            severity=perturbation.severity,
            outcome=halt,
            rung=None,
            confidence=None,
            dist_true=None,
            dist_nearest_decoy=None,
            landed_on_decoy=False,
        )

    resolution, _region = resolved
    ptn = (int(resolution.point[0]), int(resolution.point[1]))
    dist_true = _dist(ptn, true_pt)
    dist_decoy = min((_dist(ptn, d) for d in decoys), default=float("inf"))
    landed_on_decoy = dist_decoy <= tol and dist_decoy < dist_true
    outcome = Outcome.CORRECT if dist_true <= tol else Outcome.SILENT_WRONG

    return ResultRow(
        fixture_key=fixture.key(),
        family=fixture.family,
        fixture_params=fixture.params,
        perturbation=perturbation.name,
        perturbation_params=perturbation.params,
        severity=perturbation.severity,
        outcome=outcome,
        rung=resolution.rung,
        confidence=float(resolution.confidence),
        dist_true=dist_true,
        dist_nearest_decoy=(None if math.isinf(dist_decoy) else dist_decoy),
        landed_on_decoy=bool(landed_on_decoy),
    )


def sweep(
    *,
    include_ocr: bool = True,
    perturbations: Optional[list[Perturbation]] = None,
) -> list[ResultRow]:
    """Run the full deterministic grid: every fixture x every perturbation.

    ``include_ocr`` adds the OCR-tier fixtures (labeled rows / duplicate
    buttons / MRN cells) when the OCR engine + a scalable font are available;
    when they are not it silently drops to the template-tier ratchet so the
    sweep runs everywhere.
    """
    perturbations = perturbations or pt.standard_grid()
    fixtures = list(fx.template_tier_fixtures())
    if include_ocr and fx.ocr_available():
        fixtures += list(fx.ocr_tier_fixtures())
    rows: list[ResultRow] = []
    for fixture in fixtures:
        for perturbation in perturbations:
            rows.append(classify_case(fixture, perturbation))
    return rows


# --------------------------------------------------------------------------- #
# Adversarial search — an attacker that HUNTS for confident-wrong cases.
# A bounded, SEEDED random/greedy search over continuous perturbation params
# that maximizes a "danger score" (1.0 on a silent-wrong; otherwise how close a
# decoy came to out-scoring the true target). Deterministic for a fixed seed, so
# it can enrich the corpus AND run reproducibly in CI.
# --------------------------------------------------------------------------- #


@dataclass
class AdversarialHit:
    """A silent-wrong case discovered by the search."""

    fixture: Fixture
    perturbation: Perturbation
    row: ResultRow


def _danger_score(row: ResultRow, fixture: Fixture) -> float:
    """Higher = more dangerous. 1.0+ for a silent-wrong; in (0,1) when a decoy
    is nearer than it should be but the resolver still (barely) held or halted.
    """
    if row.outcome is Outcome.SILENT_WRONG:
        base = 1.0
        # Prefer high-confidence decoy hits (the truly silent ones).
        return (
            base + float(row.confidence or 0.0) + (1.0 if row.landed_on_decoy else 0.0)
        )
    if row.dist_true is not None and row.dist_nearest_decoy is not None:
        # Correct but a decoy is close — reward shrinking the margin.
        margin = row.dist_nearest_decoy - row.dist_true
        return max(0.0, 1.0 - margin / max(1.0, fixture.tolerance_px * 4))
    return 0.0


def _random_perturbation(rng: random.Random) -> Perturbation:
    """Sample a perturbation from the continuous adversarial space."""
    choice = rng.random()
    if choice < 0.4:
        return pt.compose(
            pt.occlude_target(
                kind=rng.choice(["tooltip", "cursor"]),
                coverage=round(rng.uniform(0.3, 0.95), 3),
            ),
            pt.jpeg(rng.choice([50, 20, 10])),
        )
    if choice < 0.6:
        return pt.occlude_target(
            kind=rng.choice(["tooltip", "cursor"]),
            coverage=round(rng.uniform(0.3, 0.95), 3),
        )
    if choice < 0.8:
        return pt.compose(
            pt.dpi_scale(round(rng.uniform(0.8, 1.8), 3)),
            pt.jpeg(rng.choice([50, 20, 10])),
        )
    return pt.compose(
        pt.local_drift((0.0, 0.0, round(rng.uniform(0.3, 0.7), 3), 1.0)),
        pt.jpeg(rng.choice([50, 20])),
    )


def adversarial_search(
    fixture: Fixture, *, iters: int = 40, seed: int = 0
) -> list[AdversarialHit]:
    """Bounded seeded search for silent-wrong cases on one fixture.

    Deterministic for a fixed ``seed``. Returns the distinct silent-wrong hits
    found (deduplicated by perturbation key), most-dangerous first.
    """
    # Python salts ``str.__hash__`` per process.  Using ``hash()`` here made a
    # nominally seeded search choose different perturbations across CI workers,
    # so a known corpus hit could disappear even though the resolver and seed
    # were unchanged.  Derive the PRNG seed from stable bytes instead.
    seed_material = f"{seed}\0{fixture.key()}".encode("utf-8")
    stable_seed = int.from_bytes(hashlib.sha256(seed_material).digest()[:8], "big")
    rng = random.Random(stable_seed)
    hits: dict[str, AdversarialHit] = {}
    for _ in range(iters):
        perturbation = _random_perturbation(rng)
        row = classify_case(fixture, perturbation)
        if row.outcome is Outcome.SILENT_WRONG:
            hits.setdefault(
                perturbation.key(), AdversarialHit(fixture, perturbation, row)
            )
    return sorted(
        hits.values(), key=lambda h: _danger_score(h.row, h.fixture), reverse=True
    )


# --------------------------------------------------------------------------- #
# Summary + SWER.
# --------------------------------------------------------------------------- #


@dataclass
class Summary:
    """Aggregate outcome counts + the silent-wrong rate (SWER)."""

    total: int
    counts: dict[str, int]
    silent_wrong_rate: float
    silent_wrong_rows: list[ResultRow] = field(default_factory=list)
    by_family: dict[str, dict[str, int]] = field(default_factory=dict)
    by_perturbation: dict[str, dict[str, int]] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "counts": self.counts,
            "silent_wrong": self.counts.get(Outcome.SILENT_WRONG.value, 0),
            "silent_wrong_rate": round(self.silent_wrong_rate, 4),
            "by_family": self.by_family,
            "by_perturbation": self.by_perturbation,
        }


def summarize(rows: list[ResultRow]) -> Summary:
    """Aggregate classified rows into outcome counts + SWER, decomposed."""
    counts: dict[str, int] = {o.value: 0 for o in Outcome}
    by_family: dict[str, dict[str, int]] = {}
    by_pert: dict[str, dict[str, int]] = {}
    silent: list[ResultRow] = []
    for r in rows:
        counts[r.outcome.value] += 1
        by_family.setdefault(r.family, {o.value: 0 for o in Outcome})[
            r.outcome.value
        ] += 1
        by_pert.setdefault(r.perturbation, {o.value: 0 for o in Outcome})[
            r.outcome.value
        ] += 1
        if r.outcome is Outcome.SILENT_WRONG:
            silent.append(r)
    total = len(rows)
    swer = (counts[Outcome.SILENT_WRONG.value] / total) if total else 0.0
    return Summary(
        total=total,
        counts=counts,
        silent_wrong_rate=swer,
        silent_wrong_rows=silent,
        by_family=by_family,
        by_perturbation=by_pert,
    )
