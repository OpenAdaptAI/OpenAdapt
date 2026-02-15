"""Version information for OpenAdapt.

The canonical version is managed in `pyproject.toml`. At runtime, prefer the
installed distribution version (importlib.metadata). Fall back to a dev value
when running from a source tree without an installed dist.
"""

from __future__ import annotations

import importlib.metadata


def get_version() -> str:
    """Return the installed package version, or a dev fallback."""
    try:
        return importlib.metadata.version("openadapt")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0-dev"


__version__ = get_version()
