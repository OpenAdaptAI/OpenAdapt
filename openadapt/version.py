"""Version information for the OpenAdapt meta-package.

The version is read from the installed distribution metadata (populated
from ``pyproject.toml``'s ``project.version`` at build/install time) so it
can never drift from the package's real version. semantic-release bumps
only ``pyproject.toml``; deriving ``__version__`` from metadata means the
CLI and ``openadapt.__version__`` follow automatically.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

try:
    __version__ = _dist_version("openadapt")
except PackageNotFoundError:  # pragma: no cover - source tree without install
    # Running from a source checkout that was never installed. There is no
    # metadata to read; report a clearly non-real sentinel rather than a
    # stale hardcoded number.
    __version__ = "0.0.0+unknown"
