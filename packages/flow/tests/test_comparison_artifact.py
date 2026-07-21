"""Tests for the compiled-vs-agent comparison artifact generator.

These are fast, dependency-light tests (no browser, no OCR, no network, no
model): the generator only reads the two real ``results.json`` files and emits
HTML + JSON. The tests assert that

1. the figures loaded from the source files match those files exactly (nothing
   is invented or re-derived), and
2. the emitted ``comparison.html`` actually carries the real headline figures,
   and
3. the committed HTML and JSON match a fresh generation from the raw results.
"""

from __future__ import annotations

import json
from pathlib import Path

from benchmark.comparison_artifact import generate as gen

# ---------------------------------------------------------------------------
# Extraction fidelity — the loaded figures equal the source files
# ---------------------------------------------------------------------------


def _raw(path: Path) -> dict:
    return json.loads(path.read_text())


def test_openemr_figures_match_source_exactly() -> None:
    raw = _raw(gen.OPENEMR_RESULTS)
    b = gen.load_openemr()
    for arm_name in ("compiled", "agent"):
        src = raw["arms"][arm_name]
        arm = getattr(b, arm_name)
        assert arm.n == src["n"]
        assert arm.success_count == src["success_count"]
        assert arm.p50_s == src["wall_s_p50"]
        assert arm.p95_s == src["wall_s_p95"]
        assert arm.cost_per_run == src["cost_usd_per_run"]
        assert arm.cost_total == src["cost_usd_total"]
    # The lead result's shape: compiled is free, agent is not.
    assert b.compiled.cost_per_run == 0.0
    assert b.agent.cost_per_run > 0.0
    assert b.compiled.success_count == b.compiled.n == 20
    assert b.agent.success_count == b.agent.n == 10


def test_mockmed_figures_match_source_exactly() -> None:
    raw = _raw(gen.MOCKMED_RESULTS)
    b = gen.load_mockmed()
    for arm_name in ("compiled", "agent"):
        src = raw["arms"][arm_name]
        arm = getattr(b, arm_name)
        assert arm.n == src["n"]
        assert arm.p50_s == src["wall_s_p50"]
        assert arm.cost_per_run == src["cost_usd_per_run"]
    assert b.reproducible is True
    assert b.compiled.n == 100 and b.agent.n == 20


def test_speedup_is_derived_from_real_p50s() -> None:
    b = gen.load_openemr()
    assert gen._speedup(b) == b.agent.p50_s / b.compiled.p50_s


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def test_zero_cost_formats_as_dollar_zero() -> None:
    assert gen.fmt_usd(0.0) == "$0"
    assert gen.fmt_usd_short(0.0) == "$0"


def test_nice_axis_ceils_above_max() -> None:
    axis_max, step = gen.nice_axis(0.5522)
    assert axis_max >= 0.5522
    assert step > 0


# ---------------------------------------------------------------------------
# End-to-end build — emitted HTML carries the REAL figures
# ---------------------------------------------------------------------------


def test_build_emits_html_and_json_with_real_figures(tmp_path) -> None:
    payload = gen.build(tmp_path)

    html_path = tmp_path / "comparison.html"
    json_path = tmp_path / "comparison.json"
    assert html_path.exists() and json_path.exists()

    html = html_path.read_text()

    # No model calls / no network claimed AND recorded in the payload.
    assert payload["model_calls_compiled"] == 0
    assert payload["network_calls"] == 0

    oe = gen.load_openemr()
    mm = gen.load_mockmed()

    # The real OpenEMR headline figures must appear verbatim in the page.
    for needle in (
        f"{oe.compiled.success_count}/{oe.compiled.n}",  # 20/20
        f"{oe.agent.success_count}/{oe.agent.n}",  # 10/10
        gen.fmt_s(oe.compiled.p50_s),  # 39.2 s
        gen.fmt_s(oe.agent.p50_s),  # 70.4 s
        gen.fmt_s(oe.agent.p95_s),  # 82.6 s
        gen.fmt_usd(oe.agent.cost_per_run),  # $0.5522
        gen.fmt_usd(oe.agent.cost_total, 2),  # $5.52
    ):
        assert needle in html, f"missing OpenEMR figure in HTML: {needle!r}"

    # The MockMed anchor figures too.
    for needle in (
        f"{mm.compiled.success_count}/{mm.compiled.n}",  # 100/100
        gen.fmt_s(mm.compiled.p50_s),  # 4.9 s
        gen.fmt_s(mm.agent.p50_s),  # 37.5 s
        gen.fmt_usd(mm.agent.cost_per_run),  # $0.2716
    ):
        assert needle in html, f"missing MockMed figure in HTML: {needle!r}"

    # Compiled is model-free: $0 must be shown for the compiled cost.
    assert "$0" in html
    # The wedge framing and honest caveats are present, not buried.
    assert "illustrative repeat-run model cost" in html.lower()
    assert "not new runs" in html.lower()
    assert "excludes authoring" in html.lower()
    assert "Read before quoting these numbers" in html
    assert "not a general capability claim" in html.lower() or (
        "not capability" in html.lower()
    )

    # Self-contained: no external asset references.
    for banned in ("http://", "https://cdn", "<script", 'src="http'):
        assert banned not in html, f"page is not self-contained: {banned!r}"

    # The JSON payload's figures round-trip the source files.
    payload_oe = payload["benchmarks"]["openemr"]["arms"]
    assert payload_oe["compiled"]["cost_usd_per_run"] == 0.0
    assert payload_oe["agent"]["cost_usd_per_run"] == oe.agent.cost_per_run


def test_checked_in_outputs_match_fresh_generation(tmp_path) -> None:
    """Committed publication artifacts must not lag their raw result files."""
    gen.build(tmp_path)
    for name in ("comparison.html", "comparison.json"):
        expected = (tmp_path / name).read_text()
        committed = (gen.HERE / name).read_text()
        assert committed == expected, (
            f"{name} is stale; run python -m benchmark.comparison_artifact.generate"
        )


def test_html_is_theme_aware(tmp_path) -> None:
    gen.build(tmp_path)
    html = (tmp_path / "comparison.html").read_text()
    assert "@media (prefers-color-scheme: dark)" in html
    assert 'data-theme="dark"' in html
    assert 'data-theme="light"' in html
