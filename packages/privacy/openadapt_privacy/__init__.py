"""OpenAdapt Privacy - PII/PHI detection and redaction for GUI automation data."""

import importlib
from importlib import metadata
from typing import Any

from openadapt_privacy.base import (
    Modality,
    ScrubbingProvider,
    ScrubbingProviderFactory,
    ScrubbingProviderUnavailable,
    TextScrubbingMixin,
)
from openadapt_privacy.config import PrivacyConfig, config
from openadapt_privacy.loaders import (
    Action,
    DictRecordingLoader,
    Recording,
    RecordingLoader,
    Screenshot,
    UnscrubbedScreenshot,
)
from openadapt_privacy.pipelines.dicts import DictScrubber, scrub_dict, scrub_list_dicts
from openadapt_privacy.providers import ScrubProvider

try:
    __version__ = metadata.version("openadapt-privacy")
except metadata.PackageNotFoundError:  # pragma: no cover - source checkout only
    # Never report a hard-coded version. An unmeasurable version is reported as
    # unknown so a caller cannot mistake a stale literal for the installed one.
    __version__ = "unknown"

# Names re-exported from optional-dependency modules. They are resolved lazily
# so that importing the package stays cheap, but a consumer writing
# ``from openadapt_privacy import PresidioScrubbingProvider`` must succeed
# whenever the package is installed. Before this indirection existed, that
# import raised ImportError even on a complete install, and downstream callers
# read the ImportError as "openadapt-privacy is not installed" and silently
# disabled PII/PHI scrubbing.
_LAZY_EXPORTS = {
    "PresidioScrubbingProvider": "openadapt_privacy.providers.presidio",
    "PrivacyModelUnavailable": "openadapt_privacy.providers.presidio",
}


def __getattr__(name: str) -> Any:
    """Resolve lazily re-exported provider symbols.

    Args:
        name: Attribute name being looked up on the package.

    Returns:
        The resolved attribute.

    Raises:
        AttributeError: If the name is not a package attribute.
        ImportError: If the backing module exists but cannot be imported. The
            message states explicitly that openadapt-privacy itself is
            installed, so the caller cannot mistake this for a missing package.
    """
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise ImportError(
            f"openadapt-privacy {__version__} is installed, but {name!r} could not be "
            f"imported from {module_path!r}: {exc}. Do not treat this as an absent "
            "package: scrubbing is unavailable and must not be skipped silently. "
            "Install the provider dependencies with: pip install 'openadapt-privacy[presidio]'"
        ) from exc
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Return the package attribute names, including lazy re-exports."""
    return sorted(set(globals()) | set(_LAZY_EXPORTS))

__all__ = [
    # Base classes
    "Modality",
    "ScrubbingProvider",
    "ScrubbingProviderFactory",
    "ScrubbingProviderUnavailable",
    "TextScrubbingMixin",
    # Config
    "PrivacyConfig",
    "config",
    # Providers
    "ScrubProvider",
    "PresidioScrubbingProvider",
    "PrivacyModelUnavailable",
    # Pipelines
    "DictScrubber",
    "scrub_dict",
    "scrub_list_dicts",
    # Data loaders
    "Action",
    "Screenshot",
    "Recording",
    "RecordingLoader",
    "DictRecordingLoader",
    "UnscrubbedScreenshot",
]
