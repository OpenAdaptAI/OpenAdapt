import modal

GISTING_CONDA_IMAGE = (
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

BASE_PACKAGE_LIST = [
    "accelerate==0.18.0",
    "fire==0.5.0",
    "hydra-core==1.2.0",
    "numpy==1.21.2",
    "openai==0.27.2",
    "nltk==3.6.2",
    "sentencepiece==0.1.98",
    "torch==2.0.0",
    "transformers",
    "wandb==0.13.4",
]


def create_conda_image(package: str = None, packages: list[str] = None):
    """
    Enables the user to create a conda image
    with packages of their choosing.
    """
    assert package or packages, "Please provide a package, or a list of packages"
    assert not (package and packages), "Please provide only one of: package or packages"

    if package:
        curr_packages = [package]
    else:
        curr_packages = packages

    conda_image = (
        modal.Image.conda()
        .conda_install(
            "cudatoolkit=11.2",
            "cudnn=8.1.0",
            "cuda-nvcc",
            channels=["conda-forge", "nvidia"],
        )
        .apt_install("git")
        .pip_install(packages=BASE_PACKAGE_LIST + curr_packages)
    )
    return conda_image
