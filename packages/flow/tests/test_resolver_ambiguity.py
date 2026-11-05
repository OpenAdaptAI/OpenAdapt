"""Locality + uniqueness gate for template resolution (find_template gated on
``prefer_near``, consumed by the resolution ladder's template rungs).

These exercise the REAL vision matcher (not a scripted fake): on a pixel-only
substrate a frame routinely contains several near-identical widgets, and the
raw arg-max silently clicks an ARBITRARY one. The gate must instead prefer the
instance where the target was recorded and HALT (return None) when the target
is not uniquely present where expected -- converting the most dangerous silent
mis-resolution class into a safe halt. See docs and the vision-hardening design.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from openadapt_flow.ir import Anchor
from openadapt_flow.runtime import resolver
from openadapt_flow.vision import match as match_mod
from openadapt_flow.vision.match import find_template
from openadapt_flow.vision.ocr import find_text

FONT = cv2.FONT_HERSHEY_SIMPLEX


def _png(arr: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", arr)
    assert ok
    return buf.tobytes()


def _blank(w: int = 800, h: int = 500, shade: int = 230) -> np.ndarray:
    return np.full((h, w, 3), shade, np.uint8)


def _button(img: np.ndarray, x: int, y: int, label: str, w: int = 90, h: int = 32):
    cv2.rectangle(img, (x, y), (x + w, y + h), (70, 70, 180), -1)
    cv2.rectangle(img, (x, y), (x + w, y + h), (20, 20, 20), 1)
    cv2.putText(
        img, label, (x + 8, y + h - 9), FONT, 0.5, (255, 255, 255), 1, cv2.LINE_AA
    )
    return (x, y, w, h)


def _gear(img: np.ndarray, cx: int, cy: int, r: int = 14) -> None:
    cv2.circle(img, (cx, cy), r, (70, 70, 70), -1)
    cv2.circle(img, (cx, cy), r // 2, (230, 230, 230), -1)
    for k in range(8):
        a = k * np.pi / 4
        cv2.circle(
            img,
            (int(cx + r * 1.3 * np.cos(a)), int(cy + r * 1.3 * np.sin(a))),
            3,
            (70, 70, 70),
            -1,
        )


class _Vision:
    find_template = staticmethod(find_template)
    find_text = staticmethod(find_text)


VISION = _Vision()


def _anchor(region, click, ocr_text=None, pad=400) -> Anchor:
    return Anchor(
        template="t.png",
        region=region,
        click_point=click,
        ocr_text=ocr_text,
        landmarks=[],
        search_pad=pad,
    )


# --- find_template gate (unit) ---------------------------------------------


def test_prefer_near_prefers_expected_over_higher_scoring_lookalike() -> None:
    """The expected instance wins over a strictly-HIGHER-scoring look-alike.

    The old tie-break only re-ordered matches within ~1e-3 of the best, so a
    look-alike scoring even slightly higher than the recorded target was clicked
    regardless of position. Here the expected (top) instance carries a faint 1px
    scratch (score < 1.0 but still >= threshold) while the decoy (bottom) is
    pristine (score 1.0); the gate must still choose the top.
    """
    scr = _blank()
    _button(scr, 300, 100, "Delete")  # expected (top)
    _button(scr, 300, 300, "Delete")  # pristine look-alike (bottom), scores higher
    template = scr[100:132, 300:390].copy()
    # Faintly perturb ONLY the expected instance so the decoy strictly outscores
    # it, yet it stays comfortably above threshold (a few changed pixels).
    cv2.line(scr, (305, 116), (330, 116), (60, 60, 170), 1)
    top = find_template(
        _png(scr), _png(template), search_region=(295, 95, 100, 42), threshold=0.0
    )
    bottom = find_template(
        _png(scr), _png(template), search_region=(295, 295, 100, 42), threshold=0.0
    )
    # The decoy strictly outscores the perturbed-but-detectable expected target
    # (the regime where the OLD ~1e-3 tie-break clicked the decoy).
    assert top is not None and bottom is not None
    assert bottom.confidence > top.confidence >= 0.97
    m = find_template(_png(scr), _png(template), threshold=0.97, prefer_near=(300, 100))
    assert m is not None
    assert abs(m.point[1] - 116) <= 20  # chose the TOP (expected), not the bottom


def test_ambiguous_lookalikes_none_near_expected_returns_none() -> None:
    """>= 2 peaks clear threshold and none is where expected -> HALT (None)."""
    scr = _blank(shade=235)
    for gx in (150, 300, 450, 600):
        _gear(scr, gx, 200)
    template = scr[180:220, 280:320].copy()
    # Expect the target where the target ISN'T uniquely present (all gears equal).
    m = find_template(
        _png(scr), _png(template), threshold=0.985, prefer_near=(700, 400)
    )
    assert m is None


def test_single_unique_far_match_is_kept() -> None:
    """A lone peak far from expected is a legitimately moved unique target."""
    scr = _blank()
    _button(scr, 300, 350, "Save")
    template = scr[350:382, 300:390].copy()
    m = find_template(
        _png(scr), _png(template), threshold=0.985, prefer_near=(300, 100)
    )
    assert m is not None
    assert abs(m.point[1] - 366) <= 20


def test_gate_is_inactive_without_prefer_near() -> None:
    """Postcondition matching (no prefer_near) keeps the plain arg-max match."""
    scr = _blank(shade=235)
    for gx in (150, 300, 450, 600):
        _gear(scr, gx, 200)
    template = scr[180:220, 280:320].copy()
    m = find_template(_png(scr), _png(template), threshold=0.985)
    assert m is not None  # unchanged: returns some instance, never None here


# --- resolution ladder (integration through resolve) -----------------------


def test_resolve_halts_on_occluded_target_among_identical_icons() -> None:
    """Tooltip/cursor occludes the target icon; identical decoys remain.

    Old behavior clicked a clean decoy silently; the gate now halts (all rungs
    fail) rather than resolve the wrong icon.
    """
    scr = _blank(shade=235)
    for gx in (150, 300, 450, 600):
        _gear(scr, gx, 200)
    template = scr[180:220, 280:320].copy()
    cv2.rectangle(scr, (292, 175), (320, 205), (255, 255, 0), -1)  # tooltip on target
    anchor = _anchor((280, 180, 40, 40), (300, 200), ocr_text=None, pad=400)
    result = resolver.resolve(anchor, _png(scr), VISION, template_png=_png(template))
    assert result is None


def test_resolve_picks_true_target_in_near_tie() -> None:
    """Two identical labeled buttons, both clean: resolve the recorded one."""
    scr = _blank()
    _button(scr, 300, 100, "Delete")
    _button(scr, 300, 300, "Delete")
    template = scr[100:132, 300:390].copy()
    anchor = _anchor((300, 100, 90, 32), (345, 116), ocr_text="Delete", pad=260)
    result = resolver.resolve(anchor, _png(scr), VISION, template_png=_png(template))
    assert result is not None
    resolution, _region = result
    assert abs(resolution.point[1] - 116) <= 20  # top (recorded), not bottom


def test_resolve_accepts_unique_target_at_expected() -> None:
    scr = _blank()
    _button(scr, 300, 100, "Save")
    template = scr[100:132, 300:390].copy()
    anchor = _anchor((300, 100, 90, 32), (345, 116), ocr_text="Save", pad=200)
    result = resolver.resolve(anchor, _png(scr), VISION, template_png=_png(template))
    assert result is not None
    resolution, _region = result
    assert resolution.rung == "template"
    assert abs(resolution.point[0] - 345) <= 20 and abs(resolution.point[1] - 116) <= 20


def test_local_rung_refuses_neighbor_when_target_cell_absent() -> None:
    """LOCAL rung (search_region set): the target cell is unpainted/degraded and
    the padded window catches a NEIGHBORING identical widget -> refuse (None).

    The dangerous partial-render / latency case: a lone peak inside the local
    search window that is NOT where the target was recorded is a neighbor, not
    the moved target. Committing to it is the wrong-row/wrong-icon silent click;
    the tightened rung returns None so the ladder defers to the global uniqueness
    gate (which halts) instead.
    """
    scr = _blank(shade=235)
    for gx in (150, 250, 350, 450):  # tight 100px pitch: neighbor inside the pad
        _gear(scr, gx, 200)
    template = scr[180:220, 230:270].copy()  # the gear at x=250
    # Blank the target's OWN cell (its render is pending) -> only neighbors show.
    cv2.rectangle(scr, (228, 178), (272, 222), (235, 235, 235), -1)
    m = find_template(
        _png(scr),
        _png(template),
        search_region=(180, 160, 140, 80),  # padded window around x=250
        threshold=0.985,
        prefer_near=(230, 180),
    )
    assert m is None  # refused the neighbor at x=350/150, did not click it


def test_global_suspicion_halts_on_degraded_sibling() -> None:
    """GLOBAL rung: one crisp peak far from expected, but a degraded SIBLING of
    the same repeated widget is elevated just below threshold -> refuse.

    Distinguishes 'a repeated widget whose true instance dimmed under the accept
    bar while a decoy stayed sharp' (halt) from 'a genuinely moved UNIQUE target'
    (kept, see :func:`test_single_unique_far_match_is_kept`)."""
    scr = _blank()
    _button(scr, 300, 100, "Save")  # expected location (will be blurred)
    _button(scr, 300, 350, "Save")  # crisp decoy far away
    template = scr[100:132, 300:390].copy()
    # Blur ONLY the expected instance so it drops below threshold but stays a
    # clearly-elevated sibling (scores ~0.8, above the ambiguity-suspicion floor
    # and well above background/text-fragment correlation, not a decoy tie).
    roi = scr[96:136, 296:396]
    scr[96:136, 296:396] = cv2.GaussianBlur(roi, (3, 3), 0.8)
    m = find_template(
        _png(scr), _png(template), threshold=0.985, prefer_near=(300, 100)
    )
    assert m is None  # not uniquely present: the crisp decoy is refused


def test_resolve_halts_on_unpainted_target_cell() -> None:
    """Integration: an unpainted target cell among identical icons -> HALT.

    The whole ladder must not silently click a painted neighbor when the true
    target's cell has not rendered (a partial-render / latency frame)."""
    scr = _blank(shade=235)
    for gx in (150, 250, 350, 450):
        _gear(scr, gx, 200)
    template = scr[180:220, 230:270].copy()
    cv2.rectangle(scr, (228, 178), (272, 222), (235, 235, 235), -1)  # target pending
    anchor = _anchor((230, 180, 40, 40), (250, 200), ocr_text=None, pad=120)
    result = resolver.resolve(anchor, _png(scr), VISION, template_png=_png(template))
    assert result is None


def test_near_neighbor_refused_when_widget_repeats_and_target_moved() -> None:
    """A dense list: rows 34 px apart, inside the 48 px locality radius. The
    target's row moved its button to another column; the nearest copy is the
    next row's. A moved peak is kept only when nothing else on the frame looks
    like the widget, so both template rungs refuse instead of clicking it."""
    scr = _blank(shade=235)
    for gy in (100, 236, 304):  # rows 0, 4, 6 kept their gear
        _gear(scr, 150, gy)
    _gear(scr, 600, 134)  # row 1's gear moved to another column
    template = scr[80:120, 130:170].copy()  # identical to row 1's recorded crop
    m = find_template(
        _png(scr),
        _png(template),
        search_region=(50, 34, 200, 200),
        threshold=0.985,
        prefer_near=(130, 114),  # where row 1's gear was recorded
    )
    assert m is None
    anchor = _anchor((130, 114, 40, 40), (150, 134), ocr_text=None, pad=80)
    result = resolver.resolve(anchor, _png(scr), VISION, template_png=_png(template))
    assert result is None or (
        result[0].point[0] == 600 and abs(result[0].point[1] - 134) <= 20
    )


def test_near_moved_unique_target_is_still_found() -> None:
    """The matcher may propose a unique move; it is not input authority."""
    scr = _blank(shade=235)
    _gear(scr, 150, 134 + 30)
    template = _blank(shade=235)
    _gear(template, 150, 134)
    template = template[114:154, 130:170].copy()
    m = find_template(
        _png(scr),
        _png(template),
        search_region=(50, 34, 200, 200),
        threshold=0.985,
        prefer_near=(130, 114),
    )
    assert m is not None and abs(m.point[1] - 164) <= 2
    anchor = _anchor((130, 114, 40, 40), (150, 134), pad=80)
    with pytest.raises(resolver.UncorroboratedMovedTargetRefused):
        resolver.resolve(anchor, _png(scr), VISION, template_png=_png(template))


@pytest.mark.parametrize("move", [30, 80])
def test_moved_match_refused_when_recorded_landmark_is_gone(move) -> None:
    """A crop that carries state (a checked box) matches the box now in that
    state, a row lower after a toggle. The anchor kept a label landmark that
    is not found, so the move cannot be checked: the template rungs refuse."""
    from openadapt_flow.ir import Landmark

    scr = _blank(shade=235)
    _gear(scr, 150, 134 + move)  # the matching state is near or far away
    template = _blank(shade=235)
    _gear(template, 150, 134)
    template = template[114:154, 130:170].copy()
    anchor = Anchor(
        template="t.png",
        region=(130, 114, 40, 40),
        click_point=(150, 134),
        ocr_text=None,
        landmarks=[
            Landmark(
                relation="right_of",
                ocr_text="Morning",
                distance_px=60,
                dx_px=-60,
                dy_px=0,
            )
        ],
        search_pad=80,
    )
    with pytest.raises(resolver.UncorroboratedMovedTargetRefused):
        resolver.resolve(anchor, _png(scr), VISION, template_png=_png(template))


def test_peaks_above_suppresses_neighbors() -> None:
    """The NMS peak enumerator yields one peak per identical instance."""
    scr = _blank(shade=235)
    for gx in (150, 300, 450, 600):
        _gear(scr, gx, 200)
    gray_scr = cv2.cvtColor(scr, cv2.COLOR_BGR2GRAY)
    tmpl = gray_scr[180:220, 280:320]
    result = cv2.matchTemplate(gray_scr, tmpl, cv2.TM_CCOEFF_NORMED)
    peaks = match_mod._peaks_above(result, 0.985, tmpl.shape[1], tmpl.shape[0])
    # Four identical gears -> at least four distinct strong peaks, not hundreds.
    assert 4 <= len(peaks) <= match_mod.MAX_PEAKS


# --- geometry corroborated by the control's outline -------------------------


def _checkbox(img: np.ndarray, x: int, y: int, ink, checked: bool = False) -> None:
    cv2.rectangle(img, (x, y), (x + 14, y + 14), ink, 1)
    if checked:
        cv2.line(img, (x + 3, y + 7), (x + 6, y + 11), ink, 2)
        cv2.line(img, (x + 6, y + 11), (x + 12, y + 3), ink, 2)


def _checkbox_anchor() -> Anchor:
    return _anchor((96, 96, 23, 23), (107, 107))


def test_geometry_estimate_snaps_onto_the_one_nearby_outline_in_a_dark_theme() -> None:
    """OCR read the label with the box beside it, so the landmark estimate
    landed 10 px left of the box; the box's outline (theme-independent) is
    the only one within half its size, so the click moves onto it."""
    light = _blank(shade=240)
    _checkbox(light, 100, 100, (40, 40, 40))
    template = light[96:119, 96:119].copy()
    dark = _blank(shade=30)
    _checkbox(dark, 100, 100, (220, 220, 220))
    point = resolver._outline_corroborated(
        _checkbox_anchor(), (97, 107), _png(dark), _png(template)
    )
    assert abs(point[0] - 107) <= 1 and abs(point[1] - 107) <= 1


def test_geometry_estimate_between_two_outlines_refuses() -> None:
    scr = _blank(shade=240)
    _checkbox(scr, 100, 100, (40, 40, 40))
    _checkbox(scr, 126, 100, (40, 40, 40))
    template = _blank(shade=240)
    _checkbox(template, 100, 100, (40, 40, 40))
    template = template[96:119, 96:119].copy()
    import pytest

    with pytest.raises(resolver.UncorroboratedGeometryRefused):
        resolver._outline_corroborated(
            _checkbox_anchor(), (120, 107), _png(scr), _png(template)
        )


def test_geometry_estimate_stands_when_no_outline_is_near() -> None:
    template = _blank(shade=240)
    _checkbox(template, 100, 100, (40, 40, 40))
    template = template[96:119, 96:119].copy()
    blank = _blank(shade=240)
    assert resolver._outline_corroborated(
        _checkbox_anchor(), (107, 107), _png(blank), _png(template)
    ) == (107, 107)
