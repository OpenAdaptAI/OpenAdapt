"""OpenAdapt Control Panel — a local FastAPI web UI over the OpenAdapt CLI.

The package is imported *lazily* by the meta-package's ``openadapt panel``
command, so importing this module must be cheap and side-effect free: it must
not import any sibling package (``openadapt-capture`` etc.) at module load,
mirroring the meta-package's ``doctor``/``version`` discipline. See
``openadapt_panel.system`` for how system state is inspected without executing
sibling code.
"""

from __future__ import annotations

__version__ = "0.1.0"

# Re-exported so ``openadapt/cli.py`` can wire the command with a single
# ``from openadapt_panel import run_panel``. Kept as a lazy re-export via
# __getattr__ so ``import openadapt_panel`` doesn't pull in FastAPI/uvicorn
# until the panel is actually launched.


def __getattr__(name: str):
    if name == "run_panel":
        from .server import run_panel

        return run_panel
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["run_panel", "__version__"]
