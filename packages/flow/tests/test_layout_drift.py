"""Font and layout drift detection, on synthetic lines and on real renders.

The render tests draw a synthetic back-office form in a browser, read it
with the repository OCR and check that every drift kind halts with its own
reason while an unchanged or merely shifted screen passes.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from openadapt_flow.runtime.layout_drift import (
    LayoutDriftHalt,
    LayoutFingerprint,
    check_layout,
    compare,
    fingerprint,
)


@dataclass
class L:
    text: str
    region: tuple[int, int, int, int]


LABELS = ["Account", "Holder", "Balance", "Opened"]


def lines(scale=1.0, cw=1.0, dx=0, dy=0, move=None):
    out = []
    for i, label in enumerate(LABELS):
        x, y = 40 + dx, 40 + i * 40 + dy
        if move == label:
            x += 200
        text = f"{label}: value {i}"
        out.append(
            L(
                text,
                (
                    int(x * scale),
                    int(y * scale),
                    int(len(text) * 9 * scale * cw),
                    int(18 * scale),
                ),
            )
        )
    return out


def fp(ls, viewport=(1280, 800), profile=None):
    return fingerprint(ls, viewport, landmarks=LABELS, profile_sha256=profile)


def test_same_screen_passes():
    assert not compare(fp(lines()), fp(lines())).drifted


def test_shifted_window_passes():
    assert not compare(fp(lines()), fp(lines(dx=30, dy=-12))).drifted


@pytest.mark.parametrize(
    "live,reason",
    [
        (dict(ls=lines(scale=1.25)), "font_size_changed"),
        (dict(ls=lines(cw=1.3)), "font_changed"),
        (dict(ls=lines(move="Balance")), "layout_changed"),
        (dict(ls=lines()[:2]), "too_little_text"),
        (dict(ls=lines(), viewport=(1920, 1080)), "resolution_changed"),
        (
            dict(ls=[x for x in lines() if not x.text.startswith("Holder")]),
            "landmark_missing",
        ),
        (dict(ls=lines(), profile="b" * 64), "session_profile_changed"),
    ],
)
def test_each_drift_has_its_reason(live, reason):
    rec = fp(lines(), profile="a" * 64)
    v = compare(
        rec,
        fp(
            live.get("ls"),
            live.get("viewport", (1280, 800)),
            live.get("profile", "a" * 64),
        ),
    )
    assert reason in v.reasons


def test_check_layout_halts_and_advisor_never_authorizes():
    calls = []

    def advisor(v):
        calls.append(v.reasons)
        return "labels look shifted right; re-record the form"

    rec = fp(lines())
    assert not check_layout(rec, fp(lines()), advisor=advisor).drifted
    assert calls == []  # healthy frame: no advisor call
    with pytest.raises(LayoutDriftHalt) as exc:
        check_layout(rec, fp(lines(move="Holder")), advisor=advisor)
    assert exc.value.verdict.advice and calls


def test_advisor_failure_still_halts():
    def broken(_v):
        raise RuntimeError("model unavailable")

    with pytest.raises(LayoutDriftHalt) as exc:
        check_layout(fp(lines()), fp(lines(scale=1.3)), advisor=broken)
    assert exc.value.verdict.advice == "advisor failed: RuntimeError"


def test_fingerprint_holds_no_values():
    f = fp(lines())
    assert isinstance(f, LayoutFingerprint)
    assert "value" not in repr(f.landmarks)


# -- real render + OCR ---------------------------------------------------------

FORM = """<html><body style="margin:0;background:#fff;font-family:{font};font-size:{size}px">
<div style="position:absolute;left:{left}px;top:{top}px;width:900px">
<div style="height:{row}px">Account number: AC204417</div>
<div style="height:{row}px">Holder: Brooks, Jordan</div>
<div style="height:{row}px;margin-left:{shift}px">Balance: 31597.48</div>
<div style="height:{row}px">Opened: 2021-04-09</div>
<div style="height:{row}px">Status: Active</div>
</div></body></html>"""

RLABELS = ["Account number", "Holder", "Balance", "Opened", "Status"]


def _render(font="Arial", size=16, left=40, top=40, shift=0, vw=1280, vh=800):
    pw = pytest.importorskip("playwright.sync_api")
    html = FORM.format(
        font=font, size=size, left=left, top=top, shift=shift, row=int(size * 2.2)
    )
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": vw, "height": vh})
        page.set_content(html)
        png = page.screenshot()
        b.close()
    from openadapt_flow.vision.ocr import ocr

    return fingerprint(ocr(png), (vw, vh), landmarks=RLABELS)


@pytest.fixture(scope="module")
def recorded():
    pytest.importorskip("rapidocr_onnxruntime")
    return _render()


@pytest.mark.parametrize(
    "kw,expected",
    [
        ({}, None),
        ({"left": 90, "top": 70}, None),
        ({"size": 20}, "font_size_changed"),
        ({"size": 13}, "font_size_changed"),
        ({"font": "Courier New, monospace"}, "font_changed"),
        ({"shift": 240}, "layout_changed"),
        ({"vw": 1440, "vh": 900}, "resolution_changed"),
    ],
)
def test_real_render(recorded, kw, expected):
    v = compare(recorded, _render(**kw))
    if expected is None:
        assert not v.drifted, v.details
    else:
        assert expected in v.reasons, v.details
