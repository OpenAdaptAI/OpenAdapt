"""OpenAdapt Privacy - PII/PHI detection and redaction for GUI automation data."""

import importlib
from importlib import metadata
from typing import Any

try:
    __version__ = metadata.version("openadapt-privacy")
except metadata.PackageNotFoundError:  # pragma: no cover - source checkout only
    # Never report a hard-coded version. An unmeasurable version is reported as
    # unknown so a caller cannot mistake a stale literal for the installed one.
    __version__ = "unknown"

# Re-exports stay lazy so `import openadapt_privacy.scan` does not load
# Pillow, Presidio, or spaCy. `from openadapt_privacy import X` still works.
# Presidio names were already lazy: a failed import used to be read as
# "openadapt-privacy is not installed", and callers then skipped scrubbing.
_LAZY_EXPORTS = {
    "Modality": "openadapt_privacy.base",
    "ScrubbingProvider": "openadapt_privacy.base",
    "ScrubbingProviderFactory": "openadapt_privacy.base",
    "ScrubbingProviderUnavailable": "openadapt_privacy.base",
    "TextScrubbingMixin": "openadapt_privacy.base",
    "PrivacyConfig": "openadapt_privacy.config",
    "ScrubbingPolicyChanged": "openadapt_privacy.config",
    "config": "openadapt_privacy.config",
    "Action": "openadapt_privacy.loaders",
    "DictRecordingLoader": "openadapt_privacy.loaders",
    "Recording": "openadapt_privacy.loaders",
    "RecordingLoader": "openadapt_privacy.loaders",
    "Screenshot": "openadapt_privacy.loaders",
    "UnscrubbedScreenshot": "openadapt_privacy.loaders",
    "DictScrubber": "openadapt_privacy.pipelines.dicts",
    "scrub_dict": "openadapt_privacy.pipelines.dicts",
    "scrub_list_dicts": "openadapt_privacy.pipelines.dicts",
    "ScrubProvider": "openadapt_privacy.providers",
    "PresidioScrubbingProvider": "openadapt_privacy.providers.presidio",
    "PrivacyModelUnavailable": "openadapt_privacy.providers.presidio",
}

_PRESIDIO_EXPORTS = frozenset(
    {
        "PresidioScrubbingProvider",
        "PrivacyModelUnavailable",
    }
)


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
        extra_hint = ""
        if name in _PRESIDIO_EXPORTS:
            extra_hint = (
                " Install the provider dependencies with: "
                "pip install 'openadapt-privacy[presidio]'"
            )
        raise ImportError(
            f"openadapt-privacy {__version__} is installed, but {name!r} could not be "
            f"imported from {module_path!r}: {exc}. Do not treat this as an absent "
            "package: scrubbing is unavailable and must not be skipped silently."
            f"{extra_hint}"
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
    "ScrubbingPolicyChanged",
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
