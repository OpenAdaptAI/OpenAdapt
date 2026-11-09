"""Tests for the OCR text-anchoring grounder (adopt openadapt-grounding).

``OCRAnchorGrounder`` is the PRIMARY grounding rung: it OCRs the frame via
openadapt-grounding, anchors on the row's unique key (name/MRN carried in the
step intent), and returns the target control's box on that row. Benchmark #41
(``benchmark/grounding_eval``) measured this at 88-100% on dense EMR lists where
the single-shot remote-VLM grounder scored 0/6.

Fast unit tests (no browser/OCR) cover protocol conformance, safe abstention,
the fallback chain, and the build_grounder factory. One Playwright+OCR-guarded
test renders the SAME dense surface the eval used (seed=1, n_rows=18) and
confirms the wired grounder resolves rows the bespoke grounder missed, and that
a grounder-proposed point still faces the deterministic identity band.
"""

from __future__ import annotations

import pytest

# The grounder's whole point is openadapt-grounding; without it every test here
# is moot. Skip the module rather than fail (the core stays installable).
pytest.importorskip("openadapt_grounding")

from openadapt_flow.runtime.grounder import (  # noqa: E402
    FallbackGrounder,
    Grounder,
    GrounderMatch,
    NullGrounder,
    OCRAnchorGrounder,
    build_grounder,
)


# ---------------------------------------------------------------------------
# A 1x1 white PNG (no text) — a real, decodable frame that yields no OCR boxes.
# ---------------------------------------------------------------------------
def _blank_png() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Protocol conformance + safe abstention (fast; no browser)
# ---------------------------------------------------------------------------
def test_satisfies_grounder_protocol() -> None:
    g = OCRAnchorGrounder()
    assert isinstance(g, Grounder)


def test_available_returns_instance_when_dep_present() -> None:
    assert isinstance(OCRAnchorGrounder.available(), OCRAnchorGrounder)


def test_abstains_without_ocr_text() -> None:
    # No target-control label => nothing to click => abstain (safe).
    g = OCRAnchorGrounder()
    assert g.locate(_blank_png(), intent="anything", ocr_text=None) is None
    assert g.locate(_blank_png(), intent="anything", ocr_text="  ") is None


def test_abstains_on_textless_frame() -> None:
    # A frame with no OCR text yields no boxes => no proposal (the ladder
    # halts; the remote-VLM fallback would earn its cost here).
    g = OCRAnchorGrounder()
    assert (
        g.locate(
            _blank_png(), intent="click Open for Jane Doe MRN 123", ocr_text="Open"
        )
        is None
    )


def test_abstains_on_undecodable_bytes() -> None:
    g = OCRAnchorGrounder()
    assert g.locate(b"not a png", intent="x", ocr_text="Open") is None


# ---------------------------------------------------------------------------
# Fallback chain + factory (fast; grounders stubbed)
# ---------------------------------------------------------------------------
class _StubGrounder:
    """Grounder returning a fixed match, or None, to exercise the chain."""

    def __init__(self, match):
        self._match = match
        self.calls = 0

    def locate(self, screen_png, intent, ocr_text=None):
        self.calls += 1
        return self._match


def _match() -> GrounderMatch:
    return GrounderMatch(point=(5, 5), region=(0, 0, 10, 10), confidence=0.9)


def test_fallback_returns_first_non_none() -> None:
    primary = _StubGrounder(_match())
    secondary = _StubGrounder(_match())
    chain = FallbackGrounder([primary, secondary])
    assert chain.locate(b"", "i", "o") is not None
    assert primary.calls == 1
    assert secondary.calls == 0  # short-circuits on the first proposal


def test_fallback_falls_through_to_secondary() -> None:
    primary = _StubGrounder(None)
    secondary = _StubGrounder(_match())
    chain = FallbackGrounder([primary, secondary])
    assert chain.locate(b"", "i", "o") is not None
    assert primary.calls == 1 and secondary.calls == 1


def test_fallback_abstains_when_all_abstain() -> None:
    chain = FallbackGrounder([_StubGrounder(None), _StubGrounder(None)])
    assert chain.locate(b"", "i", "o") is None
    assert isinstance(chain, Grounder)


def test_build_grounder_prefers_ocr_anchor() -> None:
    # openadapt-grounding is installed here => OCRAnchorGrounder is primary.
    g = build_grounder(fallback=None)
    assert isinstance(g, OCRAnchorGrounder)


def test_build_grounder_chains_fallback_behind_ocr() -> None:
    fallback = _StubGrounder(_match())
    g = build_grounder(fallback=fallback)
    assert isinstance(g, FallbackGrounder)
    # OCR-anchor is first; the fallback is only tried when OCR abstains.
    assert isinstance(g._grounders[0], OCRAnchorGrounder)
    assert g._grounders[1] is fallback


def test_build_grounder_none_when_ocr_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(OCRAnchorGrounder, "available", staticmethod(lambda: None))
    # No OCR, no fallback => None (equivalent to NullGrounder: no grounder rung).
    assert build_grounder(fallback=None) is None
    # No OCR but a fallback => the fallback alone is used.
    fb = _StubGrounder(_match())
    assert build_grounder(fallback=fb) is fb


def test_null_grounder_still_the_no_dep_default() -> None:
    assert NullGrounder().locate(_blank_png(), "i", "Open") is None


# ---------------------------------------------------------------------------
# Dense-list resolution — the SAME surface benchmark #41 used (seed=1, n=18).
# Guarded: needs a browser (render) + Tesseract (OCR).
# ---------------------------------------------------------------------------
pytest.importorskip("playwright")

import io  # noqa: E402

from openadapt_flow.validation.dense_surface import (  # noqa: E402
    RECORD_CONDITION,
    build_dense_table,
    render_frame,
)


def _norm_glyph(s: str) -> str:
    return (
        s.lower()
        .replace("o", "0")
        .replace("l", "1")
        .replace("i", "1")
        .replace("|", "1")
    )


@pytest.fixture(scope="module")
def dense_eval_case():
    """Render seed=1/n_rows=18 once — the eval's exact surface + targets."""
    table = build_dense_table(seed=1, n_rows=18)
    try:
        frame = render_frame(table, RECORD_CONDITION, top_offset_px=12)
    except Exception as exc:  # pragma: no cover - browser unavailable
        pytest.skip(f"browser render unavailable: {exc}")
    indices = sorted(frame.points.keys())
    step = max(1, len(indices) // 6)
    chosen = indices[::step][:6]  # [0, 8, 16, 24, 32, 40] as in the eval
    # Flag OCR-collision rows (O0/l1 MRN siblings) — those are the identity
    # band's job, not the grounder's, exactly as the REPORT documents.
    norm_counts: dict[str, int] = {}
    for i in indices:
        k = _norm_glyph(table.rows[i].mrn)
        norm_counts[k] = norm_counts.get(k, 0) + 1
    return table, frame, chosen, norm_counts


def _intent_for(row) -> str:
    return f"Click Open in the row for patient {row.name} MRN {row.mrn}"


def test_resolves_dense_clean_rows_within_tolerance(dense_eval_case) -> None:
    """On OCR-distinct rows the wired grounder hits the correct Open button —
    where the bespoke remote-VLM grounder scored 0/6 (~472 px)."""
    table, frame, chosen, norm_counts = dense_eval_case
    g = OCRAnchorGrounder()
    tol = 40  # the REPORT.md headline tolerance

    clean_hits = 0
    clean_total = 0
    any_hit_beats_baseline = False
    for i in chosen:
        row = table.rows[i]
        _, open_truth, _, _ = frame.points[i]
        collision = norm_counts[_norm_glyph(row.mrn)] > 1
        m = g.locate(frame.png, intent=_intent_for(row), ocr_text="Open")
        if collision:
            continue  # sibling separation is the identity band's job
        clean_total += 1
        assert m is not None, f"row {i} ({row.name}) got no proposal"
        err = (
            (m.point[0] - open_truth[0]) ** 2 + (m.point[1] - open_truth[1]) ** 2
        ) ** 0.5
        if err <= tol:
            clean_hits += 1
            any_hit_beats_baseline = True

    assert clean_total >= 1
    # Every OCR-distinct row must resolve within tolerance (REPORT: 100% clean).
    assert clean_hits == clean_total, f"{clean_hits}/{clean_total} clean rows hit"
    # Strictly better than the bespoke baseline's 0/6.
    assert any_hit_beats_baseline


def test_identity_band_gates_a_grounder_proposed_click(dense_eval_case) -> None:
    """A grounder PROPOSAL is not a click: the deterministic identity band still
    disposes. Same proposed point verifies against the right patient's recorded
    band and is REFUSED against a different patient's band."""
    from openadapt_flow.validation.dense_surface import (
        record_context,
        replay_observe,
    )

    table, frame, chosen, norm_counts = dense_eval_case
    g = OCRAnchorGrounder()

    # Pick two OCR-distinct target rows (a target and a different patient).
    clean = [i for i in chosen if norm_counts[_norm_glyph(table.rows[i].mrn)] == 1]
    assert len(clean) >= 2
    target_i, other_i = clean[0], clean[1]
    target = table.rows[target_i]
    other = table.rows[other_i]

    # The grounder proposes a point on the TARGET row's Open control.
    proposal = g.locate(frame.png, intent=_intent_for(target), ocr_text="Open")
    assert proposal is not None
    proposed_point = proposal.point

    # Record-time band for each patient's own Open click (mirrors the compiler).
    target_ctx, target_crop = record_context(frame, frame.points[target_i][1])
    other_ctx, other_crop = record_context(frame, frame.points[other_i][1])
    assert target_ctx and other_ctx

    # Replay-time identity verification AT THE GROUNDER'S PROPOSED POINT.
    correct = replay_observe(
        frame, proposed_point, frame.points[target_i][1], target_crop, target_ctx
    )
    wrong = replay_observe(
        frame, proposed_point, frame.points[other_i][1], other_crop, other_ctx
    )

    # Same proposed point: verifies for the correct patient, and is NOT verified
    # for the different patient (the band disposes — never a wrong-patient click).
    assert correct.check.status == "verified", (
        f"correct-patient band should verify, got {correct.check.status}"
    )
    assert wrong.check.status != "verified", (
        f"different-patient band must NOT verify at the grounder point, "
        f"got {wrong.check.status}"
    )
