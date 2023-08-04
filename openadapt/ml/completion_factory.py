from base_provider import Modality, CompletionProvider


class CompletionProviderFactory:
    def get_for_modality(modality: Modality) -> list[CompletionProvider]:
        completion_providers = CompletionProvider.get_children()
        filtered_providers = [
            provider
            for provider in completion_providers
            if modality in provider.Modalities
        ]
        return filtered_providers
