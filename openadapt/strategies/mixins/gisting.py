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
    .apt_install("git")
    .pip_install(
        "git+https://github.com/huggingface/transformers@fb366b9a2a",
        "gisting_test==0.2.3",
        "accelerate==0.18.0",
        "datasets==2.10.0",
        "deepspeed==0.8.3",
        "evaluate==0.3.0",
        "fire==0.5.0",
        "hydra-core==1.2.0",
        "numpy==1.21.2",
        "omegaconf>=2.1.1",
        "openai==0.27.2",
        "rouge_score==0.1.2",
        "nltk==3.6.2",
        "sentencepiece==0.1.98",
        "torch==2.0.0",
        "transformers",
        "wandb==0.13.4",
    )
)

stub = modal.Stub("incorporate bigger LLMS for generative mixins", image=CONDA_IMAGE)


# TODO: modify gisting_test package to return output instead of None
# m TODO: convert to mixin
@stub.function(gpu=modal.gpu.A100(count=1), timeout=1500)
def complete():
    from gisting_test.src import compress

    compress.main(
        model_name_or_path="jayelm/llama-7b-gist-1",
        base_llama_path="decapoda-research/llama-7b-hf",
        instruction="Complete the following pattern: 1, 4, 9, 16 .. with as many numbers as you can.",
    )
    return 0


@stub.local_entrypoint()
def execute():
    return complete.call()


if __name__ == "__main__":
    with stub.run():
        execute()
