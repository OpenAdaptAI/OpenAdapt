"""Compile native keyboard anchors from the event's observed focused target.

No previous click is inferred. Missing focus evidence leaves the action
unanchored; malformed native evidence refuses compilation. Identity is armed
only by the same record-context rules and marked crop used for pointer steps.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Callable

from openadapt_flow import sealed_set
from openadapt_flow.ir import Anchor, Region
from openadapt_flow.native_focus import NativeFocusTarget
from openadapt_flow.runtime.identity import (
    band_region,
    context_from_lines,
    identifier_text_from_lines,
)
from openadapt_flow.vision import imgops


def anchor_native_keyboard(
    pending: list[Any],
    *,
    bundle: Path,
    meta_identifier_region: Region | None,
    reference_date: date | None,
    cached_lines: Callable[..., Any],
) -> list[Any]:
    from openadapt_flow.compiler.compile import (
        MIN_OCR_CONFIDENCE,
        _crop_png,
        _discriminative_crop_region,
        _emit_identifier_crop,
        _identity_unarmed_reason,
        _marked_identifier_region,
    )

    result = []
    for step, before_png, after_png, event in pending:
        focus = event.get("focus_before")
        raw = focus.get("native_focus") if isinstance(focus, dict) else None
        if event.get("kind") not in {"type", "key", "hotkey"} or raw is None:
            result.append((step, before_png, after_png, event))
            continue
        try:
            target = NativeFocusTarget.model_validate(raw)
        except ValueError as exc:
            raise ValueError(
                f"invalid native focus evidence for {step.id}: {exc}"
            ) from exc
        if before_png is None:
            raise ValueError(f"native focus event {step.id} has no before frame")
        frame = imgops.decode_bgr(before_png)
        if frame is None:
            raise ValueError(
                f"native focus event {step.id} has an invalid before frame"
            )
        size = (int(frame.shape[1]), int(frame.shape[0]))
        x, y, width, height = target.region
        if x < 0 or y < 0 or x + width > size[0] or y + height > size[1]:
            raise ValueError(
                f"native focus rectangle for {step.id} is outside its before frame"
            )
        point = target.point
        crop_region = _discriminative_crop_region(frame, point)
        template = f"templates/{step.id}.png"
        sealed_set.write_bytes(bundle / template, _crop_png(before_png, crop_region))
        lines = cached_lines(int(event["i"]), "before", before_png)
        band = band_region(point, crop_region[3], size)
        context = context_from_lines(
            lines,
            exclude_region=crop_region,
            band=band,
            point=point,
            min_confidence=MIN_OCR_CONFIDENCE,
            reference_date=reference_date,
        )
        marked = _marked_identifier_region(
            event.get("identifier_region"), source=f"event {event['i']}"
        )
        identifier, region, missing = _emit_identifier_crop(
            bundle,
            step.id,
            before_png,
            frame_size=size,
            marked_region=marked if marked is not None else meta_identifier_region,
            context_text=context,
            structured_identity=None,
            frame_lines=lines,
            crop_region=crop_region,
            click=point,
            reference_date=reference_date,
        )
        if region is not None:
            marked_context = identifier_text_from_lines(
                lines,
                region=region,
                min_confidence=MIN_OCR_CONFIDENCE,
                reference_date=reference_date,
            )
            if marked_context is not None:
                context = marked_context
        armed = context is not None or identifier is not None
        anchor = Anchor(
            template=template,
            region=crop_region,
            click_point=point,
            structural=target.structural_locator(),
            context_text=context,
            identifier_crop=identifier,
            identifier_region=region,
        )
        step = step.model_copy(
            update={
                "anchor": anchor,
                "identity_armed": armed,
                "identity_unarmed_reason": None
                if armed
                else _identity_unarmed_reason(
                    lines, band=band, exclude_region=crop_region
                ),
                "identifier_crop_missing_reason": missing,
            }
        )
        result.append((step, before_png, after_png, event))
    return result
