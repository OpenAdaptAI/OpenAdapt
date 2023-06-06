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
    .pip_install(
        "tensorflow~=2.9.1",
        "torch",
        "transformers",
        "fire",
        "numpy",
        "gisting_test",
        "hydra-core==1.2.0",
        "datasets",
        "omegaconf",
        "accelerate")
)

stub = modal.Stub(
    "incorporate bigger LLMS for generative mixins",
    image = CONDA_IMAGE
)


@stub.function(gpu=modal.gpu.A100(count=2),timeout=1500)
def complete():
    import gisting_test
    from gisting_test.src import compress

    # Head over to where gisting_test
    # is installed, open compress.py and 
    # look at lines 105-107. Code added for
    # parallelism but does nothing...
    compress.main(
        "jayelm/flan-t5-xxl-gist-1",
        "Show me how to compress this")


@stub.local_entrypoint()
def execute():
    with stub.run():
        return complete.call()