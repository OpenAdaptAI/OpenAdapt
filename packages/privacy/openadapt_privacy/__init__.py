"""OpenAdapt Privacy - PII/PHI detection and redaction for GUI automation data."""

from openadapt_privacy.base import (
    Modality,
    ScrubbingProvider,
    ScrubbingProviderFactory,
    TextScrubbingMixin,
)
from openadapt_privacy.config import PrivacyConfig, config
from openadapt_privacy.loaders import (
    Action,
    DictRecordingLoader,
    Recording,
    RecordingLoader,
    Screenshot,
)
from openadapt_privacy.pipelines.dicts import DictScrubber, scrub_dict, scrub_list_dicts
from openadapt_privacy.providers import ScrubProvider

__version__ = "0.1.0"

__all__ = [
    # Base classes
    "Modality",
    "ScrubbingProvider",
    "ScrubbingProviderFactory",
    "TextScrubbingMixin",
    # Config
    "PrivacyConfig",
    "config",
    # Providers
    "ScrubProvider",
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
]
