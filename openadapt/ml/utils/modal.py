"""Conda Image creation tool."""
import modal

BASE_PACKAGE_LIST = [
    "accelerate==0.18.0",
    "fire==0.5.0",
    "numpy==1.21.2",
    "openai==0.27.2",
    "nltk==3.6.2",
    "sentencepiece==0.1.98",
    "torch==2.0.0",
    "transformers",
    "wandb==0.13.4",
]


def create_conda_image(package: str, packages: list[str]) -> modal.Image:
    """Creates conda image to use with Modal stub.

    This is useful because Modal is being used across various
    tasks in the repository for inference and fine-tuning when
    these are not possible on the user's local machine.

    The image has recommended ML packages installed inside
    BASE_PACKAGE_LIST (such as transformers and torch) and
    the purpose of this function is to add more, should the user see fit.
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
