"""Tests for the dense sibling-surface identity study.

Fast tests (no browser/OCR) cover the fixture, aggregation, and Markdown.
The end-to-end render+OCR path is exercised by one Playwright-backed test
guarded by ``importorskip`` (same pattern as the other browser tests), so the
core unit suite stays green without the OCR stack.
"""

from __future__ import annotations

import pytest

from openadapt_flow.validation import dense_surface as ds


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


def test_collision_pairs_cover_every_class() -> None:
    pairs = ds.build_collision_pairs(seed=1)
    classes = {p.collision_class for p in pairs}
    expected = {
        "near_surname",
        "nguyen_variant",
        "generational_suffix",
        "same_surname_diff_first",
        "letterletter_name",
        "same_name_diff_dob",
        "mrn_transposition",
        "id_confusion_l1",
        "id_confusion_O0",
        # 9th reopening: purely-numeric + split-numeric homonyms must be in the
        # corpus so the numeric hole can never be hidden by measurement again.
        "id_numeric_O0",
        "id_numeric_l1",
        "id_split_numeric_O0",
    }
    assert expected <= classes


def test_numeric_id_classes_are_same_name_dob_all_digit_and_collapsible() -> None:
    # The 9th-reopening additions: same name + DOB, a PURELY NUMERIC MRN
    # (no letter in the target) one O/0 or l/1 glyph-class apart -- the shape
    # the alpha-prefixed corpus hid.
    pairs = {p.collision_class: p for p in ds.build_collision_pairs(seed=2)}
    for cls in ("id_numeric_O0", "id_numeric_l1"):
        p = pairs[cls]
        assert p.target.name == p.sibling.name
        assert p.target.dob == p.sibling.dob
        assert p.target.mrn != p.sibling.mrn
        assert p.target.mrn.isdigit()  # target MRN is PURELY NUMERIC
        # the sibling swaps a confusable digit for its look-alike letter
        assert any(c in "Ol" for c in p.sibling.mrn)


def test_split_numeric_id_class_has_fragmented_confusable_mrn() -> None:
    p = {p.collision_class: p for p in ds.build_collision_pairs(seed=2)}[
        "id_split_numeric_O0"
    ]
    assert p.target.name == p.sibling.name and p.target.dob == p.sibling.dob
    assert " " in p.target.mrn  # a SPLIT (fragmented) MRN
    # each numeric fragment carries a confusable 0 the sibling renders as O
    assert "0" in p.target.mrn and "O" in p.sibling.mrn


def test_siblings_are_distinct_patients() -> None:
    # A realistic sibling is a DIFFERENT patient: never identical to the
    # target across name+dob+mrn (that would be the same entity, not a
    # collision), and the collision differs in exactly its named dimension.
    for pair in ds.build_collision_pairs(seed=3):
        t, s = pair.target, pair.sibling
        assert (t.name, t.dob, t.mrn) != (s.name, s.dob, s.mrn)


def test_confusable_id_classes_differ_by_one_char() -> None:
    pairs = {p.collision_class: p for p in ds.build_collision_pairs(seed=2)}
    for cls in ("id_confusion_l1", "id_confusion_O0"):
        p = pairs[cls]
        # same name + DOB, MRN one character apart (the identifier collision)
        assert p.target.name == p.sibling.name
        assert p.target.dob == p.sibling.dob
        assert p.target.mrn != p.sibling.mrn
        assert len(p.target.mrn) == len(p.sibling.mrn)
        diffs = sum(a != b for a, b in zip(p.target.mrn, p.sibling.mrn))
        assert diffs == 1


def test_dense_table_keeps_all_pairs_and_pads() -> None:
    table = ds.build_dense_table(seed=5, n_rows=40)
    ids = {id(r) for r in table.rows}
    for pair in table.pairs:
        assert id(pair.target) in ids
        assert id(pair.sibling) in ids
    # n_rows is a floor: never truncates below the natural length.
    assert table.n_rows >= 40
    assert table.n_rows == len(table.rows)


def test_render_html_places_click_targets() -> None:
    table = ds.build_dense_table(seed=1, n_rows=40)
    html = ds.render_table_html(
        table, font_family="Arial", font_px=15, row_pad_px=6, top_offset_px=4
    )
    assert 'data-name="0"' in html and 'data-open="0"' in html
    # every seeded collision name is present in the DOM
    for pair in table.pairs:
        assert pair.sibling.name.split(",")[0] in html


# ---------------------------------------------------------------------------
# Aggregation / Markdown (synthetic trial records, no browser)
# ---------------------------------------------------------------------------


def _trial(**kw):
    base = dict(
        seed=1,
        collision_class="near_surname",
        note="",
        click_config="click_name",
        replay_condition="native_arial",
        target_name="A, X",
        sibling_name="B, Y",
        target_mrn="M1",
        sibling_mrn="M2",
        target_dob="1980-01-01",
        sibling_dob="1980-01-01",
        armed=True,
        context_text="M1 1980-01-01 M Active",
        fa_status="verified",
        fa_coverage=1.0,
        fa_observed="M1 ...",
        fa_used_upscale=False,
        fa_surname_readable=True,
        fa_status_no_rowfilter="verified",
        is_false_abort=False,
        acc_status="mismatch",
        acc_coverage=0.5,
        acc_observed="M2 ...",
        acc_expected="M1 ...",
        acc_used_upscale=False,
        is_false_accept=False,
        bleed_neighbor_tokens=[],
        bleed_present=False,
        bleed_survived_rowfilter=False,
        bleed_changed_fa_verdict=False,
    )
    base.update(kw)
    return base


def _result(trials):
    return {
        "trials": trials,
        "meta": {
            "seeds": [1],
            "n_rows": 40,
            "replay_conditions": ["native_arial"],
            "record_condition": "record",
            "click_configs": list(ds.CLICK_CONFIGS),
            "operating_point": {},
        },
    }


def test_aggregate_counts_false_abort_and_accept() -> None:
    trials = [
        _trial(),  # clean
        _trial(fa_status="mismatch", is_false_abort=True),
        _trial(fa_status="unreadable", is_false_abort=True),
        _trial(
            acc_status="verified",
            is_false_accept=True,
            collision_class="id_confusion_O0",
        ),
    ]
    agg = ds.aggregate(_result(trials))
    h = agg["headline"]
    assert h["n"] == 4
    assert h["false_abort"] == 2
    assert h["false_abort_mismatch"] == 1
    assert h["false_abort_unreadable"] == 1
    assert h["false_accept"] == 1
    assert len(agg["false_accept_details"]) == 1
    assert agg["false_accept_details"][0]["collision_class"] == "id_confusion_O0"


def test_aggregate_excludes_unarmed_from_rates() -> None:
    trials = [_trial(), _trial(armed=False, context_text=None, is_false_abort=False)]
    agg = ds.aggregate(_result(trials))
    assert agg["headline"]["n"] == 1  # unarmed excluded from rate denominator
    assert agg["unarmed_count"] == 1


def test_markdown_reports_false_accept_prominently() -> None:
    trials = [
        _trial(
            acc_status="verified",
            is_false_accept=True,
            collision_class="id_confusion_O0",
            acc_expected="COX3834 1944-08-08",
            acc_observed="COX3834 1944-08-08",
        )
    ]
    agg = ds.aggregate(_result(trials))
    md = ds.render_markdown(_result(trials), agg)
    assert "FALSE ACCEPT" in md
    assert "COX3834" in md
    assert "id_confusion_O0" in md


def test_markdown_zero_false_accept_states_held() -> None:
    agg = ds.aggregate(_result([_trial()]))
    md = ds.render_markdown(_result([_trial()]), agg)
    assert "Zero." in md
    # synthetic ROC baseline cited; tracks SYNTHETIC_FALSE_ABORT (9th reopening:
    # numeric MRNs now abstain, raising it to 48.31%).
    assert f"{ds.SYNTHETIC_FALSE_ABORT * 100:.2f}%" in md
    assert "48.31%" in md


def test_bleed_aggregation() -> None:
    trials = [
        _trial(bleed_present=True, bleed_neighbor_tokens=["Foo"]),
        _trial(
            bleed_present=True,
            bleed_survived_rowfilter=True,
            bleed_changed_fa_verdict=True,
        ),
    ]
    agg = ds.aggregate(_result(trials))
    assert agg["bleed"]["bleed_present"] == 2
    assert agg["bleed"]["bleed_survived_rowfilter"] == 1
    assert agg["bleed"]["bleed_changed_fa_verdict"] == 1


# ---------------------------------------------------------------------------
# End-to-end render + OCR (Playwright + RapidOCR)
# ---------------------------------------------------------------------------


def test_end_to_end_record_and_replay_faithful() -> None:
    pytest.importorskip("playwright.sync_api")
    pytest.importorskip("rapidocr_onnxruntime")

    table = ds.build_dense_table(seed=1, n_rows=30)
    rec = ds.render_frame(table, ds.RECORD_CONDITION, top_offset_px=0)
    rep = ds.render_frame(table, ds.REPLAY_CONDITIONS[1], top_offset_px=5)
    idx = {id(r): i for i, r in enumerate(table.rows)}

    pair = table.pairs[0]  # near_surname
    ti, si = idx[id(pair.target)], idx[id(pair.sibling)]
    click = rec.points[ti][0]
    ctx, crop = ds.record_context(rec, click)
    assert ctx is not None  # dense clinical row arms identity

    # True target row verifies (no false abort on the clean crisp/native pair).
    fa = ds.replay_observe(rep, rep.points[ti][0], click, crop, ctx)
    assert fa.check.status == "verified"

    # Adjacent sibling (distinct MRN) is NOT verified as the target.
    ac = ds.replay_observe(rep, rep.points[si][0], click, crop, ctx)
    assert ac.check.status != "verified"
