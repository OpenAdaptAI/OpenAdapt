from .base import Modality, ScrubbingProvider


class ScrubbingProviderFactory:
    """A Factory Class for Scrubbing Providers"""

    @staticmethod
    def get_for_modality(modality: Modality) -> list[ScrubbingProvider]:
        scrubbing_providers = ScrubbingProvider.__subclasses__()
        filtered_providers = [
            provider()
            for provider in scrubbing_providers
            if modality in provider().capabilities
        ]
        return filtered_providers


class PresidioScrubbingProvider(ScrubbingProvider):
    """A Class for Presidio Scrubbing Provider"""

    def scrub_text(self, input: str) -> str:
        pass  # Implemented for Presidio
