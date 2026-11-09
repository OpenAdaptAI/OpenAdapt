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

from openadapt_flow.ir import Anchor, Point, Region
from openadapt_flow.runtime import identity as identity_mod
from openadapt_flow.runtime.healing.governance import (
    BandVerifier,
    _default_band_verifier,
)
from openadapt_flow.runtime.healing.patch import HealPatch, IdentitySnapshot


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
    construction_error: Optional[str] = None


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


def identity_row_region(anchor: Anchor, viewport: tuple[int, int]) -> Region:
    """Bound the complete target and its declared OCR identity in one row strip."""
    regions = [anchor.region]
    regions.append(
        anchor.identifier_region
        or identity_mod.band_region(anchor.click_point, anchor.region[3], viewport)
    )
    if any(
        x < 0 or y < 0 or x + width > viewport[0] or y + height > viewport[1]
        for x, y, width, height in regions
    ):
        raise ValueError("target or identity region is outside the evidence frame")
    top = min(region[1] for region in regions)
    bottom = max(region[1] + region[3] for region in regions)
    return (0, top, viewport[0], bottom - top)


def perturbation_set(
    frame_png: bytes,
    anchor: Anchor,
    *,
    kinds: Optional[tuple[DriftKind, ...]] = None,
) -> list[DriftCase]:
    """Build all requested conditions; an invalid fixture remains a failed case."""
    kinds = kinds or tuple(DriftKind)
    target = anchor.click_point
    cases: list[DriftCase] = []
    for kind in kinds:
        if kind != DriftKind.REFLOW:
            cases.append(perturb(frame_png, target, kind))
            continue
        try:
            with Image.open(io.BytesIO(frame_png)) as frame:
                viewport = frame.size
            _, top, _, height = identity_row_region(anchor, viewport)
            if top + height + 24 > viewport[1]:
                raise ValueError("reflow would clip the target or identity evidence")
            # Insert above the complete row, never through the target or its
            # identity text. The fold at the click point split glyphs in half.
            cases.append(perturb(frame_png, target, kind, reflow_from_y=top))
        except ValueError as exc:
            cases.append(
                DriftCase(
                    "reflow", kind, frame_png, target, construction_error=str(exc)
                )
            )
    return cases


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
    identity_anchor: Optional[Anchor] = None,
) -> HarnessReport:
    """Replay a candidate patch against a drift battery + prior traces.

    For each case the harness checks the patch's repaired target both
    LOCATES (``resolve`` returns a point within ``locate_tolerance`` of the
    case's known post-drift target) and VERIFIES its identity (the band read
    at that point still matches the patch's post-heal ``context_text``). A
    patch that fails either on any case is not promotable -- the same
    identity-never-weakened rule the gate enforces, now across synthetic
    drift.

    Only genuinely unarmed patches skip identity verification. A hashed or
    structured identity snapshot without its verification material fails closed.
    ``identity_anchor`` supplies the complete repaired identity evidence; its
    projection must match the patch. The caller supplies the complete anchor
    from its integrity-verified candidate bundle.
    """
    if identity_anchor is not None and (
        IdentitySnapshot.from_anchor(identity_anchor) != patch.identity_after
    ):
        raise ValueError("repair identity anchor does not match the patch snapshot")
    expected_band = patch.identity_after.context_text
    results: list[CaseResult] = []
    for case in cases:
        if case.construction_error is not None:
            results.append(
                CaseResult(
                    case.label,
                    case.kind,
                    located=False,
                    identity_ok=False,
                    detail=f"invalid drift fixture: {case.construction_error}",
                )
            )
            continue
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

        observed = sample_band(case.frame_png, located_point) or ""
        if identity_anchor is not None:
            status = anchor_band_verdict(identity_anchor, observed, band_verifier)
        elif expected_band:
            status = band_verifier(expected_band, observed)
        elif (
            patch.identity_after.armed
            or patch.identity_after.has_identity_template
            or patch.identity_after.identifier_crop
            or patch.identity_after.identifier_region
        ):
            status = "unreadable"
        else:
            status = "unarmed"
        results.append(
            CaseResult(
                case.label,
                case.kind,
                located=True,
                identity_ok=(status in ("verified", "unarmed")),
                detail=f"band verdict {status!r}",
            )
        )
    return HarnessReport(results=results)


def anchor_band_verdict(
    anchor: Anchor,
    observed: str,
    band_verifier: BandVerifier = _default_band_verifier,
) -> str:
    """Apply the runtime OCR identity check without restoring plaintext identity.

    Pixel-only or structured-only evidence cannot be verified by this OCR
    campaign. Keep it unreadable rather than treating its missing context as
    an unarmed target. The runtime's hashed OCR tier takes precedence over a
    plaintext context, so a conflicting context cannot replace its identity.
    """
    template = anchor.identity_template
    if template is not None and template.tokens:
        from openadapt_flow.runtime.identity_template import verify_template_identity

        return verify_template_identity(template, observed).status
    if anchor.context_text:
        return band_verifier(anchor.context_text, observed)
    if (
        template is not None
        or anchor.structured_identity
        or anchor.identifier_crop
        or anchor.identifier_region
    ):
        return "unreadable"
    return "unarmed"


def band_sampler(
    viewport: tuple[int, int], vision: object, *, anchor: Optional[Anchor] = None
) -> SampleBandFn:
    """Read the runtime's OCR identity evidence around a resolved target.

    A supplied anchor binds the identifier region or excludes the target's own
    mutable label, as runtime verification does. Keep recorded offsets and
    dimensions unchanged: the runtime OCR tier translates them but does not
    scale them, even when a campaign frame has a different viewport size.
    """
    from datetime import date

    def sample(frame_png: bytes, point: Point) -> Optional[str]:
        with Image.open(io.BytesIO(frame_png)) as frame:
            live_viewport = frame.size
        today = date.today()
        if anchor is not None:

            def translated(region: Region) -> Region:
                x, y, width, height = region
                return (
                    point[0] + x - anchor.click_point[0],
                    point[1] + y - anchor.click_point[1],
                    width,
                    height,
                )

            if anchor.identifier_region is not None:
                region = translated(anchor.identifier_region)
                return identity_mod.identifier_text_from_lines(
                    vision.ocr(frame_png),  # type: ignore[attr-defined]
                    region=region,
                    reference_date=today,
                )
            height = anchor.region[3]
            exclude = translated(anchor.region)
        else:
            height = 64
            exclude = None
        band = identity_mod.band_region(point, height, live_viewport)
        lines = [
            line
            for line in vision.ocr(frame_png, region=band)  # type: ignore[attr-defined]
            if line.text.strip()
            and (
                exclude is None
                or not identity_mod.regions_intersect(line.region, exclude)
            )
            and not identity_mod.is_volatile_line(line.text, reference_date=today)
        ]
        lines = identity_mod.lines_near_point(lines, point[1])
        return " ".join(line.text.strip() for line in lines) or None

    return sample
