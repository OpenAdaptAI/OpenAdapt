from base_provider import CompletionProvider, Modality, Capability, Availability


class GPT3CompletionProvider(CompletionProvider):
    Name = "GPT3-Davinci"
    Capabilities = [
        Capability.TRAINING,
        Capability.TUNING,
        Capability.INFERENCE,
    ]
    Modalities = [Modality.TEXT]
    Availabilities = [Availability.HOSTED]

    def finetune(self, prompt_completion_pair: list[dict[str]]):
        # TODO
        pass

    def infer(self, prompt: str):
        # TODO add call to local finetuned model for inference
        pass
