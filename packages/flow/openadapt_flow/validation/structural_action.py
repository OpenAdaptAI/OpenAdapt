"""Availability probe: the structural ACTION rung vs the visual ladder alone.

Mirrors the desktop-benchmark finding that reframed the thesis from
"vision-only" to "deterministic compiled automation with visual FALLBACK":
structural (DOM/UIA) execution scored 21/21 while compiled *visual* replay
scored 6/21 under render drift. This probe reproduces that shape on a real
rendered DOM with the real resolution ladder.

Model
-----
A dense surface of ``n`` actionable targets (buttons), each with a STABLE DOM
id (``#open-pK``) -- exactly what a browser recorder captures for the
structural rung -- AND a recorded visual anchor (template crop at the button's
recorded position + its OCR label). We then RE-RENDER the surface with drift:
a deterministic subset of targets is left untouched (models steps whose surface
did not change between record and replay), while the rest are MOVED and
RELABELLED and RE-THEMED (models real render drift: layout shift, an Open->View
relabel, a dark theme). The DOM id never changes.

For every target we ask, on the drifted surface:

* STRUCTURAL rung -- ``backend.locate_structural(locator)``: does it land inside
  the CORRECT element's box? (Deterministic; independent of pixels.)
* VISUAL ladder -- ``resolve(anchor, drift_png, vision, structural=None)``:
  does the template/OCR/geometry ladder resolve to a point inside the CORRECT
  element's box? A drifted target's recorded "Open" crop no longer matches at
  its old position and either fails to resolve or matches a look-alike sibling
  -- both are availability/correctness failures.

The numbers are MEASURED (never hardcoded): structural resolves every target
whose id is still in the DOM; visual resolves only the non-drifted minority.

Runnable via benchmark/structural_action/structural_action_probe.py; the
reusable logic (build_html / run_probe) also backs tests/test_structural_rung.py.
"""

from __future__ import annotations

import io
from typing import Optional

from PIL import Image

from openadapt_flow import vision as vision_mod
from openadapt_flow.backends.playwright_backend import PlaywrightBackend
from openadapt_flow.ir import Anchor
from openadapt_flow.runtime.resolver import resolve

# Grid layout (viewport is the PlaywrightBackend default 1280x800).
_COL_X = 90  # baseline button column (left)
_DRIFT_COL_X = 560  # drifted buttons jump to a second column
_TOP = 70
_ROW_H = 34
_BTN_W = 84
_BTN_H = 24


def _is_stable(index: int, n: int) -> bool:
    """Deterministic 'did not drift' subset (~every 4th target).

    Models the minority of steps whose surface is unchanged between record and
    replay. Chosen to land the visual-only score near the 6/21 illustration
    without ever inspecting resolution outcomes.
    """
    return index % 4 == 0


def _button_html(index: int, *, drift: bool) -> str:
    stable = _is_stable(index, 0)
    left = _COL_X
    label = "Open"
    cls = "open-btn"
    if drift and not stable:
        left = _DRIFT_COL_X
        label = "View"  # rename drift (label no longer matches crop/OCR)
        cls = "open-btn drifted"  # dark-theme restyle
    top = _TOP + index * _ROW_H
    return (
        f'<button class="{cls}" id="open-p{index}" data-id="p{index}" '
        f'style="position:absolute;left:{left}px;top:{top}px;'
        f'width:{_BTN_W}px;height:{_BTN_H}px" '
        f'aria-label="{label} patient p{index}">{label}</button>'
    )


def build_html(n: int, *, drift: bool) -> str:
    """A dense absolutely-positioned surface of ``n`` actionable buttons."""
    buttons = "\n".join(_button_html(i, drift=drift) for i in range(n))
    return (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        "html,body{margin:0;padding:0;background:#f4f6f8;"
        "font-family:Arial,Helvetica,sans-serif}"
        ".open-btn{font-size:13px;border:1px solid #256;background:#e8f0ff;"
        "color:#123;border-radius:4px;cursor:pointer}"
        ".open-btn.drifted{background:#222a33;color:#eef;border-color:#8ab;"
        "font-family:Georgia,'Times New Roman',serif;font-size:15px}"
        "</style></head><body>"
        f"{buttons}"
        "</body></html>"
    )


def _crop_png(png: bytes, box: tuple[int, int, int, int]) -> bytes:
    x, y, w, h = box
    with Image.open(io.BytesIO(png)) as img:
        crop = img.crop((x, y, x + w, y + h))
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        return buf.getvalue()


def _bbox(backend: PlaywrightBackend, selector: str) -> Optional[tuple]:
    box = backend.page.locator(selector).bounding_box()
    if not box:
        return None
    return (box["x"], box["y"], box["width"], box["height"])


def _inside(point, box) -> bool:
    if box is None:
        return False
    px, py = point
    x, y, w, h = box
    return x <= px <= x + w and y <= py <= y + h


def run_probe(n: int = 21, *, headless: bool = True) -> dict:
    """Record on a baseline surface, drift it, compare structural vs visual.

    Returns a report dict with per-target outcomes and the two success counts.
    """
    backend, close = PlaywrightBackend.launch("about:blank", headless=headless)
    try:
        # -- record on the baseline surface --------------------------------
        backend.page.set_content(build_html(n, drift=False))
        backend.page.wait_for_timeout(60)
        base_png = backend.screenshot()
        anchors: list[Anchor] = []
        locators = []
        for i in range(n):
            box = _bbox(backend, f"#open-p{i}")
            assert box is not None, f"missing #open-p{i} on baseline"
            cx = int(box[0] + box[2] / 2)
            cy = int(box[1] + box[3] / 2)
            region = (
                int(box[0]) - 6,
                int(box[1]) - 6,
                int(box[2]) + 12,
                int(box[3]) + 12,
            )
            locator = backend.structural_locator_at(cx, cy)
            assert locator is not None, f"no structural locator for p{i}"
            locators.append(locator)
            anchors.append(
                Anchor(
                    template=f"templates/open-p{i}.png",
                    region=region,
                    click_point=(cx, cy),
                    ocr_text="Open",
                    structural=locator,
                )
            )

        templates = {i: _crop_png(base_png, anchors[i].region) for i in range(n)}

        # -- drift the surface ---------------------------------------------
        backend.page.set_content(build_html(n, drift=True))
        backend.page.wait_for_timeout(60)
        drift_png = backend.screenshot()

        targets = []
        structural_ok = 0
        visual_ok = 0
        for i in range(n):
            correct_box = _bbox(backend, f"#open-p{i}")

            # structural rung (deterministic, id-based)
            handle = backend.locate_structural(locators[i])
            s_ok = handle is not None and _inside(handle.point, correct_box)

            # visual ladder only (structural disabled)
            res = resolve(
                anchors[i],
                drift_png,
                vision_mod,
                template_png=templates[i],
                viewport=backend.viewport,
                structural=None,
            )
            v_ok = res is not None and _inside(res[0].point, correct_box)

            structural_ok += int(s_ok)
            visual_ok += int(v_ok)
            targets.append(
                {
                    "id": f"open-p{i}",
                    "drifted": not _is_stable(i, n),
                    "structural_ok": bool(s_ok),
                    "structural_rung": handle is not None,
                    "visual_ok": bool(v_ok),
                    "visual_rung": (res[0].rung if res else None),
                }
            )

        return {
            "n": n,
            "structural_ok": structural_ok,
            "visual_ok": visual_ok,
            "structural_ratio": f"{structural_ok}/{n}",
            "visual_ratio": f"{visual_ok}/{n}",
            "targets": targets,
        }
    finally:
        close()
