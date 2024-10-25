"""Download the spacy model."""

import json
import os
import pathlib
import shutil

import git
import requests

from openadapt import PROJECT_DIR
from openadapt.build_utils import get_root_dir_path


def download_spacy_model(model_name: str) -> None:
    """Download the spacy model.

    Args:
        model_name: The name of the model to download.
    """
    preferences_folder = get_root_dir_path()
    version = None

    def download_latest_model_version() -> str:
        """Download the latest version of the model.

        - Clone the model from the huggingface repository to a temp directory.
        - Create a directory for the model in the preferences folder.
        - Copy the meta.json file to the model directory.
        - Copy the contents of the temp directory to the model directory using the
        version in the meta.json file as the name of the directory.
        - Remove the temp directory.
        - Recursively loop over all files in the model directory and copy the contents,
        some files are git lfs files and so the contents need to be downloaded
        separately.
        """
        git.Repo.clone_from(
            f"https://huggingface.co/spacy/{model_name}",
            preferences_folder / f"{model_name}-temp",
        )
        os.mkdir(preferences_folder / model_name)
        with open(preferences_folder / f"{model_name}/__init__.py", "w") as f:
            f.write("")
        shutil.copy(
            preferences_folder / f"{model_name}-temp/meta.json",
            preferences_folder / f"{model_name}/meta.json",
        )
        with open(preferences_folder / f"{model_name}/meta.json", "r") as f:
            metadata = json.load(f)
            version = metadata["version"]
        # copy contents of temp directory to the
        # model_name/model_name-{version} directory
        shutil.copytree(
            preferences_folder / f"{model_name}-temp",
            preferences_folder / f"{model_name}/{model_name}-{version}",
        )
        # remove the temp directory
        shutil.rmtree(preferences_folder / f"{model_name}-temp")
        # loop over all files in the model_name/model_name-{version} directory
        model_dir = preferences_folder / f"{model_name}/{model_name}-{version}"
        for dirpath, _, filenames in os.walk(
            preferences_folder / f"{model_name}/{model_name}-{version}"
        ):
            if ".git" in dirpath:
                continue
            for filename in filenames:
                if filename.endswith(".whl"):
                    continue
                relative_path = f"{pathlib.Path(dirpath).relative_to(model_dir)}/{filename}".replace(
                    "\\", "/"
                ).replace(
                    "./", ""
                )
                url = (
                    "https://huggingface.co/spacy/en_core_web_trf/resolve/main/"
                    f"{relative_path}"
                )
                # overwrite the file with the contents of the file at the url
                with open(
                    preferences_folder
                    / f"{model_name}/{model_name}-{version}/{relative_path}",
                    "wb",
                ) as f:
                    f.write(requests.get(url).content)
        return version

    if not (preferences_folder / model_name).exists():
        version = download_latest_model_version()
    else:
        version = requests.get(
            f"https://huggingface.co/spacy/{model_name}/resolve/main/meta.json"
        ).json()["version"]
        downloaded_version = json.load(
            open(preferences_folder / f"{model_name}/meta.json")
        )["version"]
        if version != downloaded_version:
            shutil.rmtree(preferences_folder / model_name)
            version = download_latest_model_version()

    parent_dir = PROJECT_DIR.parent
    # copy the model_name directory to the parent_dir
    shutil.copytree(preferences_folder / model_name, parent_dir / model_name)
    shutil.copy(
        pathlib.Path(__file__).parent / "spacy_model_init.py",
        parent_dir / model_name / "__init__.py",
    )
    os.mkdir(parent_dir / f"{model_name}-{version}.dist-info")
