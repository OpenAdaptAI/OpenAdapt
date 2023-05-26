import modal
import transformers
import io
import time

CONDA_IMAGE = (
    modal.Image.conda()
    .conda_install(
        "cudatoolkit=11.2",
        "cudnn=8.1.0",
        "cuda-nvcc",
        channels=["conda-forge", "nvidia"],
    )
    .pip_install("tensorflow~=2.9.1","torch","transformers")
)

stub = modal.Stub(
    "incorporate bigger LLMS for generative mixins",
    image = CONDA_IMAGE
)

# CAREFUL NOT TO RUN OUT OF CREDITS!
# GPUs on Modal are billed per hour of usage.

# Note that this function can be modified to fit
# potentially larger LLMs, but cannot be coded
# to generalize to all generative models as syntax and params differ.

@stub.function(gpu="A10G",timeout=900)
def complete(prompt: str, max_tokens: int, model_name: str):

    tokenizer = transformers.AutoTokenizer.from_pretrained(model_name)
    model = transformers.AutoModelForCausalLM.from_pretrained(model_name)
    input_tokens = tokenizer(prompt, return_tensors="pt")
    pad_token_id = tokenizer.eos_token_id
    attention_mask = input_tokens["attention_mask"]
    output_tokens = model.generate(
        input_ids=input_tokens["input_ids"],
        attention_mask=attention_mask,
        max_length=input_tokens["input_ids"].shape[-1] + max_tokens,
        pad_token_id=pad_token_id,
        num_return_sequences=1
        )
    N = input_tokens["input_ids"].shape[-1]
    completion = tokenizer.decode(
        output_tokens[:, N:][0],
        clean_up_tokenization_spaces=True,
    )
    return completion

@stub.local_entrypoint()
def execute(prompt: str, max_tokens: int, model_name: str):
    with stub.run():
        return complete.call(prompt, max_tokens, model_name)