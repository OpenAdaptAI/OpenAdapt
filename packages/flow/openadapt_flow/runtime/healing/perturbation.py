"""Deterministic synthetic UI-drift perturbation + regression harness.

A heal claims "the target moved / re-themed / re-flowed and my refreshed
anchor still finds and verifies it." This harness lets us CHECK that claim
before promoting the patch, without a live app: it generates deterministic,
``$0`` synthetic drifts of a recorded frame (shifted / scaled / re-themed /
re-flowed) with a KNOWN post-drift target location, then replays a candidate
patch against them plus any prior recorded traces and reports whether the
patch still (a) locates the target and (b) verifies its identity band under
every drift. A patch that regresses on any case is not promotable.

Reusable by design: held-out identity validation and future patch-induction
both reuse the same generator and the same replay/report, so a patch is
judged by exactly the drift battery the runtime will face.

Image work is PIL-only and seeded -- fully deterministic; no model calls.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from PIL import Image, ImageOps

from openadapt_flow.ir import Anchor, Point
from openadapt_flow.runtime import identity as identity_mod
from openadapt_flow.runtime.healing.governance import (
    BandVerifier,
    _default_band_verifier,
)
from openadapt_flow.runtime.healing.patch import HealPatch


class DriftKind(str, Enum):
    """Synthetic UI-drift classes the harness reproduces."""

    SHIFT = "shift"  # whole frame translated (window/panel moved)
    SCALE = "scale"  # frame content zoomed (DPI / browser zoom)
    RETHEME = "retheme"  # palette inverted (dark theme) -- target stays put
    REFLOW = "reflow"  # content below a fold pushed down (inserted row/banner)


@dataclass
class DriftCase:
    """One drift instance: a frame and where the target is IN that frame."""

    label: str
    kind: DriftKind
    frame_png: bytes
    expected_point: Point


@dataclass
class CaseResult:
    label: str
    kind: DriftKind
    located: bool
    identity_ok: bool
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.located and self.identity_ok


@dataclass
class HarnessReport:
    """Aggregate regression report over a drift battery."""

    results: list[CaseResult] = field(default_factory=list)

    @property
    def promotable(self) -> bool:
        return bool(self.results) and all(r.passed for r in self.results)

    @property
    def failures(self) -> list[CaseResult]:
        return [r for r in self.results if not r.passed]

    def summary(self) -> str:
        n_pass = sum(1 for r in self.results if r.passed)
        return (
            f"{n_pass}/{len(self.results)} drift cases passed "
            f"(promotable={self.promotable})"
        )


def _open(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png)).convert("RGB")


def _dump(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _clamp(point: Point, size: tuple[int, int]) -> Point:
    return (
        max(0, min(point[0], size[0] - 1)),
        max(0, min(point[1], size[1] - 1)),
    )


def perturb(
    frame_png: bytes,
    target: Point,
    kind: DriftKind,
    *,
    shift: Point = (17, 11),
    scale: float = 1.15,
    reflow_from_y: Optional[int] = None,
    reflow_dy: int = 24,
) -> DriftCase:
    """Produce one deterministic drift of ``frame_png`` around ``target``.

    Returns a :class:`DriftCase` whose ``expected_point`` is where the target
    lands after the drift -- the ground truth a patch must still resolve to.
    """
    image = _open(frame_png)
    w, h = image.size

    if kind is DriftKind.SHIFT:
        dx, dy = shift
        out = Image.new("RGB", (w, h), (255, 255, 255))
        out.paste(image, (dx, dy))
        point = _clamp((target[0] + dx, target[1] + dy), (w, h))
        return DriftCase("shift", kind, _dump(out), point)

    if kind is DriftKind.SCALE:
        sw, sh = max(1, int(w * scale)), max(1, int(h * scale))
        out = image.resize((sw, sh))
        point = _clamp((int(target[0] * scale), int(target[1] * scale)), (sw, sh))
        return DriftCase("scale", kind, _dump(out), point)

    if kind is DriftKind.RETHEME:
        # Palette inversion (a dark theme): geometry is untouched, so the
        # target does not move -- the classic re-theme heal case.
        out = ImageOps.invert(image)
        return DriftCase("retheme", kind, _dump(out), _clamp(target, (w, h)))

    if kind is DriftKind.REFLOW:
        # Everything at/below the fold is pushed down by reflow_dy (an
        # inserted banner/row). The target moves iff it is below the fold.
        fold = reflow_from_y if reflow_from_y is not None else target[1] - 1
        out = image.copy()
        below = image.crop((0, fold, w, h))
        out.paste((255, 255, 255), (0, fold, w, h))
        out.paste(below, (0, min(h - 1, fold + reflow_dy)))
        moved = target[1] >= fold
        point = _clamp((target[0], target[1] + (reflow_dy if moved else 0)), (w, h))
        return DriftCase("reflow", kind, _dump(out), point)

    raise ValueError(f"unknown drift kind {kind!r}")


def perturbation_set(
    frame_png: bytes,
    anchor: Anchor,
    *,
    kinds: Optional[tuple[DriftKind, ...]] = None,
) -> list[DriftCase]:
    """The full deterministic drift battery for an anchor's target point."""
    kinds = kinds or tuple(DriftKind)
    target = anchor.click_point
    return [perturb(frame_png, target, kind) for kind in kinds]


# Resolve a target in a (possibly drifted) frame -> its point, or None if the
# patch can no longer find it. Injected: tests pass a fake; production can pass
# a resolver-backed closure. Kept off the runtime hot path (promotion is
# offline / canary), so a model-backed resolver is allowed HERE but never
# required.
ResolveFn = Callable[[bytes], Optional[Point]]
#: Read the identity band around a point in a frame -> its text (or None).
SampleBandFn = Callable[[bytes, Point], Optional[str]]


def replay_patch(
    patch: HealPatch,
    cases: list[DriftCase],
    *,
    resolve: ResolveFn,
    sample_band: SampleBandFn,
    band_verifier: BandVerifier = _default_band_verifier,
    locate_tolerance: int = 6,
) -> HarnessReport:
    """Replay a candidate patch against a drift battery + prior traces.

    For each case the harness checks the patch's repaired target both
    LOCATES (``resolve`` returns a point within ``locate_tolerance`` of the
    case's known post-drift target) and VERIFIES its identity (the band read
    at that point still matches the patch's post-heal ``context_text``). A
    patch that fails either on any case is not promotable -- the same
    identity-never-weakened rule the gate enforces, now across synthetic
    drift.

    Unarmed patches (no post-heal context band) skip the identity leg: there
    was no band to preserve (the governance gate has already ensured such a
    patch did not DROP an armed band).
    """
    expected_band = patch.identity_after.context_text
    results: list[CaseResult] = []
    for case in cases:
        located_point = resolve(case.frame_png)
        located = located_point is not None and (
            abs(located_point[0] - case.expected_point[0]) <= locate_tolerance
            and abs(located_point[1] - case.expected_point[1]) <= locate_tolerance
        )
        if not located:
            results.append(
                CaseResult(
                    case.label,
                    case.kind,
                    located=False,
                    identity_ok=False,
                    detail=f"target not located (got {located_point})",
                )
            )
            continue

        if not expected_band:
            results.append(
                CaseResult(
                    case.label,
                    case.kind,
                    located=True,
                    identity_ok=True,
                    detail="unarmed patch: no identity band to verify",
                )
            )
            continue

        observed = sample_band(case.frame_png, located_point) or ""
        status = band_verifier(expected_band, observed)
        results.append(
            CaseResult(
                case.label,
                case.kind,
                located=True,
                identity_ok=(status == "verified"),
                detail=f"band verdict {status!r}",
            )
        )
    return HarnessReport(results=results)


def band_sampler(viewport: tuple[int, int], vision: object) -> SampleBandFn:
    """A :data:`SampleBandFn` that reads the OCR identity band via ``vision``.

    Mirrors the replayer's own band read (full-width band at the anchor
    height around the point, volatile lines dropped against today's date), so
    the harness verifies identity by the same rule the pre-click gate uses.
    Provided as a convenience for wiring the harness to a real vision object;
    tests inject a simpler fake.
    """
    from datetime import date

    def sample(frame_png: bytes, point: Point) -> Optional[str]:
        band = identity_mod.band_region(point, 64, viewport)
        today = date.today()
        lines = [
            line
            for line in vision.ocr(frame_png, region=band)  # type: ignore[attr-defined]
            if line.text.strip()
            and not identity_mod.is_volatile_line(line.text, reference_date=today)
        ]
        lines = identity_mod.lines_near_point(lines, point[1])
        return " ".join(line.text.strip() for line in lines) or None

    return sample
