"""Replay and fault campaigns for a repair candidate.

Two evidence campaigns stand between a candidate and approval:

- the **replay campaign** replays the repaired binding against HEALTHY cases:
  the evidence frame itself plus the deterministic synthetic drift battery
  (:mod:`openadapt_flow.runtime.healing.perturbation`, reused verbatim). The
  repaired binding must locate the target and verify its identity band on
  every case.
- the **fault campaign** replays the repaired binding against ADVERSARIAL
  cases: ambiguity (a duplicated target), wrong entity (the identity band
  names a different entity), stale target (the target is gone), unexpected
  dialog (an interstitial covers the target), and verifier failure (the
  identity band is unreadable). The repaired binding must REFUSE on every
  case: either fail to locate, or locate and fail identity verification so
  the pre-click gate halts. Acting confidently on any fault case is a silent
  wrong action and fails the campaign.

Both runners are deterministic, model-free, and injectable (``resolve`` /
``sample_band`` callables), mirroring the existing perturbation harness so
tests are hermetic and production wires the real resolver and OCR.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from enum import Enum

from PIL import Image, ImageDraw

from openadapt_flow.ir import Anchor, Point, Region
from openadapt_flow.repair.candidate import CampaignCaseResult, CampaignResult
from openadapt_flow.runtime.healing.governance import (
    BandVerifier,
    _default_band_verifier,
)
from openadapt_flow.runtime.healing.patch import HealPatch, IdentitySnapshot
from openadapt_flow.runtime.healing.perturbation import (
    DriftCase,
    DriftKind,
    ResolveFn,
    SampleBandFn,
    perturbation_set,
    replay_patch,
)


class FaultKind(str, Enum):
    """Adversarial fault classes a candidate must refuse on."""

    AMBIGUITY = "ambiguity"
    WRONG_ENTITY = "wrong_entity"
    STALE_TARGET = "stale_target"
    UNEXPECTED_DIALOG = "unexpected_dialog"
    VERIFIER_FAILURE = "verifier_failure"


FAULT_BATTERY_KINDS: tuple[FaultKind, ...] = tuple(FaultKind)


@dataclass
class FaultCase:
    """One adversarial frame the repaired binding must refuse on."""

    label: str
    kind: FaultKind
    frame_png: bytes
    #: Where the (possibly counterfeit) target sits in this frame. Used only
    #: for diagnostics; ANY confident act on a fault frame is a failure.
    target_point: Point


def _open(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png)).convert("RGB")


def _dump(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _band_region(region: Region, size: tuple[int, int]) -> Region:
    """A full-width band at the target's rows (the identity band's home)."""
    width, _height = size
    return (0, region[1], width, region[3])


def _fault_ambiguity(image: Image.Image, region: Region) -> Image.Image:
    """Duplicate the target ON ITS OWN ROW so the frame holds two identical,
    identically-banded hits (the truly dangerous ambiguity)."""
    x, y, w, h = region
    crop = image.crop((x, y, x + w, y + h))
    out = image.copy()
    # Paste at the horizontally mirrored column; when that overlaps the
    # original column, shift right instead (wrapped inside the frame).
    dx = max(0, min(image.width - w, image.width - w - x))
    if abs(dx - x) < w + 8:
        dx = (x + 2 * w + 16) % max(1, image.width - w)
    out.paste(crop, (dx, y))
    return out


def _fault_wrong_entity(image: Image.Image, region: Region) -> Image.Image:
    """Rewrite the identity band rows so they name a DIFFERENT entity."""
    out = image.copy()
    band = _band_region(region, out.size)
    x, y, w, h = band
    draw = ImageDraw.Draw(out)
    tx, ty, tw, th = region
    # Blank the band rows around the target, keep the target pixels intact
    # (the locator still matches; only the identity evidence changes).
    draw.rectangle((x, y, x + w, y + h), fill=(255, 255, 255))
    out.paste(image.crop((tx, ty, tx + tw, ty + th)), (tx, ty))
    draw.text(
        (max(4, tx - 160), y + max(0, h // 4)),
        "WRONG ENTITY 1999-12-31",
        fill=(0, 0, 0),
    )
    return out


def _fault_stale_target(image: Image.Image, region: Region) -> Image.Image:
    """Remove the target: the recorded control no longer exists."""
    out = image.copy()
    x, y, w, h = region
    ImageDraw.Draw(out).rectangle((x, y, x + w, y + h), fill=(255, 255, 255))
    return out


def _fault_unexpected_dialog(image: Image.Image, region: Region) -> Image.Image:
    """Cover the target (and beyond) with an opaque modal dialog."""
    out = image.copy()
    x, y, w, h = region
    pad = 24
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1 = min(out.width, x + w + pad)
    y1 = min(out.height, y + h + pad)
    draw = ImageDraw.Draw(out)
    draw.rectangle((x0, y0, x1, y1), fill=(230, 230, 230), outline=(90, 90, 90))
    draw.text((x0 + 8, y0 + 8), "Unsaved changes. Continue?", fill=(0, 0, 0))
    return out


def _fault_verifier_failure(image: Image.Image, region: Region) -> Image.Image:
    """Make the identity band unreadable while the target stays present."""
    out = image.copy()
    band = _band_region(region, out.size)
    x, y, w, h = band
    draw = ImageDraw.Draw(out)
    draw.rectangle((x, y, x + w, y + h), fill=(128, 128, 128))
    tx, ty, tw, th = region
    out.paste(image.crop((tx, ty, tx + tw, ty + th)), (tx, ty))
    return out


def fault_battery(
    frame_png: bytes,
    anchor: Anchor,
    *,
    kinds: tuple[FaultKind, ...] = FAULT_BATTERY_KINDS,
) -> list[FaultCase]:
    """The deterministic adversarial battery for one repaired anchor."""
    image = _open(frame_png)
    region = anchor.region
    point = anchor.click_point
    builders = {
        FaultKind.AMBIGUITY: _fault_ambiguity,
        FaultKind.WRONG_ENTITY: _fault_wrong_entity,
        FaultKind.STALE_TARGET: _fault_stale_target,
        FaultKind.UNEXPECTED_DIALOG: _fault_unexpected_dialog,
        FaultKind.VERIFIER_FAILURE: _fault_verifier_failure,
    }
    cases: list[FaultCase] = []
    for kind in kinds:
        drifted = builders[kind](image, region)
        cases.append(
            FaultCase(
                label=kind.value,
                kind=kind,
                frame_png=_dump(drifted),
                target_point=point,
            )
        )
    return cases


def _patch_for_anchor(step_id: str, anchor: Anchor) -> HealPatch:
    """A minimal HealPatch view of a repaired anchor for the drift harness."""
    snapshot = IdentitySnapshot.from_anchor(anchor)
    return HealPatch(
        step_id=step_id,
        rung_used="template",
        identity_before=snapshot,
        identity_after=snapshot,
    )


def run_replay_campaign(
    step_id: str,
    anchor: Anchor,
    frame_png: bytes,
    *,
    resolve: ResolveFn,
    sample_band: SampleBandFn,
    band_verifier: BandVerifier = _default_band_verifier,
    kinds: tuple[DriftKind, ...] | None = None,
) -> CampaignResult:
    """Replay the repaired binding against the healthy-case battery.

    Healthy cases are the evidence frame itself (baseline) plus the reused
    deterministic drift battery. The binding must locate the target and
    verify its identity on EVERY case.
    """
    baseline = DriftCase(
        label="baseline",
        kind=DriftKind.SHIFT,
        frame_png=frame_png,
        expected_point=anchor.click_point,
    )
    cases = [baseline] + perturbation_set(frame_png, anchor, kinds=kinds)
    report = replay_patch(
        _patch_for_anchor(step_id, anchor),
        cases,
        resolve=resolve,
        sample_band=sample_band,
        band_verifier=band_verifier,
    )
    case_results = [
        CampaignCaseResult(
            label=f"{step_id}:{result.label}",
            kind=result.kind.value,
            passed=result.passed,
            detail=result.detail,
        )
        for result in report.results
    ]
    failed = sum(1 for result in case_results if not result.passed)
    return CampaignResult(
        campaign="replay",
        passed=bool(case_results) and failed == 0,
        total=len(case_results),
        failed=failed,
        cases=case_results,
    )


def run_fault_campaign(
    step_id: str,
    anchor: Anchor,
    frame_png: bytes,
    *,
    resolve: ResolveFn,
    sample_band: SampleBandFn,
    band_verifier: BandVerifier = _default_band_verifier,
    kinds: tuple[FaultKind, ...] = FAULT_BATTERY_KINDS,
) -> CampaignResult:
    """Replay the repaired binding against the adversarial battery.

    A case PASSES only when the binding REFUSES: the resolver finds nothing
    (or raises a refusal), or it finds a target whose identity band does NOT
    verify, so the pre-click identity gate halts the run. A case FAILS when
    the binding would confidently act: the resolver returns a point AND the
    identity band verifies (or there is no identity band to check at all,
    which on an adversarial frame is a silent wrong action).
    """
    expected_band = anchor.context_text
    case_results: list[CampaignCaseResult] = []
    for case in fault_battery(frame_png, anchor, kinds=kinds):
        label = f"{step_id}:{case.label}"
        try:
            located_point = resolve(case.frame_png)
        except Exception as exc:
            case_results.append(
                CampaignCaseResult(
                    label=label,
                    kind=case.kind.value,
                    passed=True,
                    detail=f"refused: resolver raised {type(exc).__name__}",
                )
            )
            continue
        if located_point is None:
            case_results.append(
                CampaignCaseResult(
                    label=label,
                    kind=case.kind.value,
                    passed=True,
                    detail="refused: target not located",
                )
            )
            continue
        if not expected_band:
            case_results.append(
                CampaignCaseResult(
                    label=label,
                    kind=case.kind.value,
                    passed=False,
                    detail=(
                        "acted without an identity band on an adversarial "
                        "frame (unarmed binding cannot pass the fault campaign)"
                    ),
                )
            )
            continue
        observed = sample_band(case.frame_png, located_point) or ""
        status = band_verifier(expected_band, observed)
        if status == "verified":
            case_results.append(
                CampaignCaseResult(
                    label=label,
                    kind=case.kind.value,
                    passed=False,
                    detail=(
                        "silent wrong action: located the target and the "
                        f"identity band verified on a {case.kind.value} frame"
                    ),
                )
            )
        else:
            case_results.append(
                CampaignCaseResult(
                    label=label,
                    kind=case.kind.value,
                    passed=True,
                    detail=f"refused: identity band verdict {status!r}",
                )
            )
    failed = sum(1 for result in case_results if not result.passed)
    return CampaignResult(
        campaign="fault",
        passed=bool(case_results) and failed == 0,
        total=len(case_results),
        failed=failed,
        cases=case_results,
    )


def merge_campaign_results(results: list[CampaignResult]) -> CampaignResult:
    """Fold per-step campaign results into one recorded campaign verdict."""
    if not results:
        raise ValueError("cannot merge an empty campaign result list")
    campaign = results[0].campaign
    if any(result.campaign != campaign for result in results):
        raise ValueError("cannot merge results from different campaign kinds")
    cases = [case for result in results for case in result.cases]
    failed = sum(1 for case in cases if not case.passed)
    return CampaignResult(
        campaign=campaign,
        passed=bool(cases) and failed == 0,
        total=len(cases),
        failed=failed,
        cases=cases,
    )
