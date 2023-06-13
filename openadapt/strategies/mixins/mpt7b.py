"""
Implements a rudimentary mixin for using
MPT-7B to generate completions.


"""
#from openadapt.strategies.base import BaseReplayStrategy
#from openadapt.models import Recording
import transformers
import modal

CONDA_IMAGE = (
    modal.Image.conda()
    .conda_install(
        "cudatoolkit=11.2",
        "cudnn=8.1.0",
        "cuda-nvcc",
        channels=["conda-forge", "nvidia"],
    )
    .pip_install("tensorflow~=2.9.1","torch","transformers", "einops")
)

stub = modal.Stub(
    "incorporate bigger LLMS for generative mixins",
    image = CONDA_IMAGE
)

@stub.cls(gpu=modal.gpu.A100(memory=20))
class MPT_7BReplayStrategy():
    def __enter__(self):
        import transformers

        self.model = transformers.AutoModelForCausalLM.from_pretrained(
            'mosaicml/mpt-7b',
            trust_remote_code=True
        )
        self.tokenizer = transformers.AutoTokenizer.from_pretrained(
            'EleutherAI/gpt-neox-20b')

    @modal.method()
    def get_completions(self, prompt: str,  max_input_size: int):
        if max_input_size > 2048:
            max_input_size = 2048

        prompt = prompt[-max_input_size:]
        input_tokens = self.tokenizer(prompt, return_tensors="pt")
        input_ids = input_tokens['input_ids']

        
        output = self.model.generate(input_ids)

        completion = self.tokenizer.decode(output[0])

        print(completion)
        return completion

@stub.local_entrypoint()
def ex():
    print("Hello")
    test = MPT_7BReplayStrategy()
    test.get_completions.call("Melbourne is a ", 2048)
    




