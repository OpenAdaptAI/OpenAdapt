from ml_models import CompletionProvider

class GPT3CompletionProvider(CompletionProvider):
    name = "GPT3-Davinci"

    def finetune(self, prompt: str, completion: str):
        # TODO
        pass

    def infer(self, prompt: str):
        # TODO add call to local finetuned model for inference
        pass

        