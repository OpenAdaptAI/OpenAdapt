"""Unit tests for the robust benchmark-chart font helper.

The benchmark chart is cosmetic; a font-lookup failure must never fail the
suite. These tests cover the two guarantees in
``openadapt_flow.benchmark.chart_fonts``: the bundled DejaVuSans font is
registered so ``findfont`` cannot miss, and ``safe_render`` swallows any
rendering failure.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openadapt_flow.benchmark.chart_fonts import (
    BUNDLED_FONT_NAME,
    configure_bundled_font,
    safe_render,
)


def test_configure_registers_bundled_font_and_returns_pyplot() -> None:
    """The bundled DejaVuSans TTF is registered and pinned as the default."""
    plt = configure_bundled_font()
    # Returned object is pyplot on the (headless) Agg backend.
    assert hasattr(plt, "subplots")
    import matplotlib

    assert matplotlib.get_backend().lower() == "agg"
    assert plt.rcParams["font.sans-serif"][0] == BUNDLED_FONT_NAME

    from matplotlib import font_manager

    ttf = Path(matplotlib.get_data_path()) / "fonts" / "ttf" / "DejaVuSans.ttf"
    assert ttf.is_file()
    # The exact bundled family is known to the in-memory font manager, so a
    # findfont lookup resolves without touching the fragile on-disk cache.
    names = {f.name for f in font_manager.fontManager.ttflist}
    assert BUNDLED_FONT_NAME in names


def test_configure_lets_a_real_chart_render(tmp_path: Path) -> None:
    """A chart renders end-to-end after configuration (no font error)."""
    plt = configure_bundled_font()
    fig, ax = plt.subplots()
    ax.bar(["a", "b"], [1, 2])
    ax.set_title("cosmetic")
    out = tmp_path / "chart.png"
    fig.savefig(out)
    plt.close(fig)
    assert out.stat().st_size > 0


def test_safe_render_returns_value_on_success() -> None:
    """``safe_render`` forwards args and returns the callable's result."""
    result = safe_render(lambda a, b: a + b, 2, b=3)
    assert result == 5


def test_safe_render_swallows_failure_and_logs() -> None:
    """A rendering failure is caught, logged, and returns ``None``."""
    logged: list[str] = []

    def boom(*_a: Any, **_k: Any) -> None:
        raise ValueError("Failed to find font DejaVu Sans")

    result = safe_render(boom, "x", log=logged.append)
    assert result is None
    assert logged and "skipped" in logged[0]
