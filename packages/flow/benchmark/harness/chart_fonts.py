"""Robust matplotlib font setup for (cosmetic) benchmark charts.

Benchmark charts are cosmetic; the product of a benchmark run is the numeric
``results.json``. Chart text must therefore not depend on matplotlib's shared,
on-disk font cache, which fresh venvs and concurrent runs can leave empty or
half-written -- producing ``ValueError: Failed to find font DejaVu Sans`` from
``matplotlib.font_manager.findfont``.

matplotlib *ships* a ``DejaVuSans.ttf`` inside its own wheel
(``mpl-data/fonts/ttf/DejaVuSans.ttf``). :func:`configure_bundled_font`
registers that exact file with the in-memory font manager and pins it as the
default sans-serif family, so ``findfont`` resolves against the registered
entry instead of the fragile cache and can no longer miss.

:func:`safe_render` is the belt-and-suspenders: if matplotlib is missing or
anything about font setup / rendering still fails, the chart is skipped
(returns ``None``) and the benchmark's numeric results are left untouched.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# The font matplotlib bundles in its wheel; always present, never cache-gated.
BUNDLED_FONT_NAME = "DejaVu Sans"


def configure_bundled_font() -> Any:
    """Configure matplotlib (Agg) with the wheel-bundled DejaVuSans font.

    Registers the ``DejaVuSans.ttf`` that ships inside matplotlib's own data
    directory with the in-memory font manager and pins it as the default
    sans-serif family, so ``findfont`` cannot miss even when the on-disk font
    cache is empty or corrupt (fresh venvs / concurrent runs).

    Returns:
        The imported ``matplotlib.pyplot`` module (Agg backend).
    """
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import font_manager

    font_path = Path(matplotlib.get_data_path()) / "fonts" / "ttf" / "DejaVuSans.ttf"
    if font_path.is_file():
        try:
            font_manager.fontManager.addfont(str(font_path))
        except Exception:  # noqa: BLE001 - registration is best-effort
            logger.debug("could not register bundled font %s", font_path)

    import matplotlib.pyplot as plt

    existing = [
        f for f in plt.rcParams.get("font.sans-serif", []) if f != BUNDLED_FONT_NAME
    ]
    plt.rcParams["font.family"] = "sans-serif"
    plt.rcParams["font.sans-serif"] = [BUNDLED_FONT_NAME, *existing]
    # A missing glyph for the Unicode minus is another cosmetic-only failure.
    plt.rcParams["axes.unicode_minus"] = False
    return plt


def safe_render(
    render: Callable[..., Any],
    *args: Any,
    log: Optional[Callable[[str], None]] = None,
    **kwargs: Any,
) -> Optional[Any]:
    """Run a chart-render callable, swallowing cosmetic rendering failures.

    A benchmark chart is a nice-to-have; a font-lookup or matplotlib failure
    must never fail the benchmark. On any exception the chart is skipped and
    ``None`` is returned; the numeric ``results.json`` is written by the caller
    independently.

    Args:
        render: The chart-render callable (e.g. ``render_chart``).
        *args: Positional arguments forwarded to ``render``.
        log: Optional logging callable; defaults to this module's logger.
        **kwargs: Keyword arguments forwarded to ``render``.

    Returns:
        Whatever ``render`` returns on success, else ``None``.
    """
    try:
        return render(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - chart is cosmetic, never fatal
        msg = (
            "benchmark chart skipped (cosmetic; numeric results unaffected): "
            f"{type(exc).__name__}: {exc}"
        )
        if log is not None:
            log(msg)
        else:
            logger.warning(msg)
        return None
